#include <Arduino.h>
#include <cstring>
#include <math.h>
#include "driver/i2s.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

// ===== Serial and common =====
static constexpr int SERIAL_BAUD    = 921600;

// ===== MIC -> HOST config =====
static constexpr int MIC_SAMPLE_RATE   = 16000;   // capture rate
static constexpr int CHUNK_SAMPLES     = 1024;    // per outbound chunk
static constexpr uint32_t FLUSH_MS     = 10;      // time-based flush to bound latency

// XIAO ESP32S3 MIC pins (ICS-43434)
static constexpr int PIN_MIC_BCLK = 3;  // D2 -> GPIO3
static constexpr int PIN_MIC_WS   = 1;  // D0 -> GPIO1 (LRCL)
static constexpr int PIN_MIC_SD   = 6;  // D5 -> GPIO6 (DOUT)
static constexpr int PIN_MIC_SEL  = 4;  // D3 -> GPIO4 (SEL, LOW selects left)

static constexpr i2s_port_t I2S_PORT_MIC = I2S_NUM_1;

// ===== HOST -> SPEAKER config =====
static constexpr i2s_port_t I2S_PORT_SPK = I2S_NUM_0;

// XIAO ESP32S3 speaker pins (MAX98357A or similar)
static constexpr int PIN_SPK_BCLK     = 8;   // D9 -> GPIO8
static constexpr int PIN_SPK_WS       = 7;   // D8 -> GPIO7
static constexpr int PIN_SPK_SDOUT    = 9;   // D10 -> GPIO9
static constexpr int PIN_AMP_SHUTDOWN = 44;  // D7 -> GPIO44 (active HIGH)

// ===== MIC state =====
static int16_t  streamBuffer[CHUNK_SAMPLES];
static size_t   streamSamples = 0;
static uint32_t lastFlushMs   = 0;

// ===== SPEAKER state =====
static bool     spkI2SInstalled   = false;
static int      spkSampleRateHz   = 22050;
static uint32_t samplesRemaining  = 0;
static QueueHandle_t spkEventQueue = nullptr;
static uint32_t spkSamplesInFlight = 0;
static bool playbackDonePending    = false;

enum InboundState {
  WAITING_HEADER,
  STREAMING_PCM,
  WAITING_FOOTER
};
static InboundState inboundState = WAITING_HEADER;
static char headerBuffer[64];
static size_t headerIndex = 0;
static char footerBuffer[8];
static size_t footerIndex = 0;

// ===== Helpers =====
static void sendLine(const char *line) {
  Serial.write(line);
  Serial.write('\n');
}

static void publishState() {
  sendLine("STATE STREAMING");
}

// ----- MIC side -----
static void setupI2SMicrophone() {
  i2s_config_t config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
      .sample_rate = MIC_SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,          // 24-bit data in 32-bit slot
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = false,
      .fixed_mclk = 0
  };

  i2s_pin_config_t pinConfig = {
      .mck_io_num   = I2S_PIN_NO_CHANGE,
      .bck_io_num   = PIN_MIC_BCLK,
      .ws_io_num    = PIN_MIC_WS,
      .data_out_num = I2S_PIN_NO_CHANGE,
      .data_in_num  = PIN_MIC_SD
  };

  ESP_ERROR_CHECK(i2s_driver_install(I2S_PORT_MIC, &config, 0, nullptr));
  ESP_ERROR_CHECK(i2s_set_pin(I2S_PORT_MIC, &pinConfig));
  ESP_ERROR_CHECK(i2s_set_clk(I2S_PORT_MIC, MIC_SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_MONO));
}

static void sendAudioChunk() {
  if (streamSamples == 0) return;
  const size_t byteCount = streamSamples * sizeof(int16_t);
  char header[24];
  snprintf(header, sizeof(header), "AUDIO %zu", byteCount);
  sendLine(header);
  Serial.write(reinterpret_cast<uint8_t *>(streamBuffer), byteCount);
  streamSamples = 0;
  lastFlushMs = millis();
}

static void processMicFrames(const int32_t *frames, size_t count) {
  for (size_t i = 0; i < count; ++i) {
    // ICS-43434 left-justified 24-bit inside 32-bit word. Shift to 16-bit signed PCM.
    int16_t s16 = static_cast<int16_t>(frames[i] >> 12);
    streamBuffer[streamSamples++] = s16;
    if (streamSamples == CHUNK_SAMPLES) sendAudioChunk();
  }
}

// ----- SPEAKER side -----
static void configureI2SSpeaker(int sampleRate) {
  spkSampleRateHz = sampleRate;

  if (spkI2SInstalled) {
    i2s_stop(I2S_PORT_SPK);
    i2s_driver_uninstall(I2S_PORT_SPK);
    spkI2SInstalled = false;
    spkEventQueue = nullptr;
    spkSamplesInFlight = 0;
    playbackDonePending = false;
  }

  i2s_config_t config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = sampleRate,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0
  };

  i2s_pin_config_t pinConfig = {
      .mck_io_num   = I2S_PIN_NO_CHANGE,
      .bck_io_num   = PIN_SPK_BCLK,
      .ws_io_num    = PIN_SPK_WS,
      .data_out_num = PIN_SPK_SDOUT,
      .data_in_num  = I2S_PIN_NO_CHANGE
  };

  if (i2s_driver_install(I2S_PORT_SPK, &config, 4, &spkEventQueue) != ESP_OK) {
    sendLine("LOG Failed to install I2S TX");
    return;
  }
  if (i2s_set_pin(I2S_PORT_SPK, &pinConfig) != ESP_OK) {
    sendLine("LOG Failed to set I2S TX pins");
    return;
  }
  i2s_set_clk(I2S_PORT_SPK, sampleRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  i2s_zero_dma_buffer(I2S_PORT_SPK);
  i2s_start(I2S_PORT_SPK);

  spkI2SInstalled = true;
  spkSamplesInFlight = 0;
  playbackDonePending = false;
  if (spkEventQueue) {
    xQueueReset(spkEventQueue);
  }
}

static void playBootTone() {
  if (!spkI2SInstalled) return;
  const float freq = 440.0f;
  const float twoPi = 6.28318530718f;
  const int durationMs = 150;
  const int total = (spkSampleRateHz * durationMs) / 1000;
  static int16_t buf[128];

  int produced = 0;
  while (produced < total) {
    int frames = min<int>(128, total - produced);
    for (int i = 0; i < frames; ++i) {
      float t = float(produced + i) / float(spkSampleRateHz);
      buf[i] = int16_t(8000.0f * sinf(twoPi * freq * t));
    }
    size_t written = 0;
    i2s_write(I2S_PORT_SPK, buf, frames * sizeof(int16_t), &written, portMAX_DELAY);
    produced += frames;
  }
}

static void pollSpeakerEvents() {
  if (!spkEventQueue) {
    if (playbackDonePending && samplesRemaining == 0) {
      sendLine("PLAYBACK_DONE");
      playbackDonePending = false;
    }
    return;
  }

  i2s_event_t event;
  while (xQueueReceive(spkEventQueue, &event, 0) == pdTRUE) {
    if (event.type == I2S_EVENT_TX_DONE) {
      uint32_t frames = event.size / sizeof(int16_t);
      if (frames >= spkSamplesInFlight) {
        spkSamplesInFlight = 0;
      } else {
        spkSamplesInFlight -= frames;
      }
    }
  }

  if (playbackDonePending && samplesRemaining == 0 && spkSamplesInFlight == 0 && inboundState == WAITING_HEADER) {
    sendLine("PLAYBACK_DONE");
    playbackDonePending = false;
  }
}

static void handleHeaderByte(char c) {
  if (c == '\n') {
    headerBuffer[headerIndex] = '\0';

    if (strcmp(headerBuffer, "STATE?") == 0) {
      publishState();
    } else {
      int sampleRate = 0, channels = 0, bits = 0;
      uint32_t sampleCount = 0;
      int scanned = sscanf(headerBuffer, "START %d %d %d %u", &sampleRate, &channels, &bits, &sampleCount);
      if (scanned == 4 && channels == 1 && bits == 16 && sampleCount > 0) {
        configureI2SSpeaker(sampleRate);
        samplesRemaining = sampleCount;
        inboundState = STREAMING_PCM;
        Serial.printf("Streaming %u mono samples at %d Hz\n", sampleCount, sampleRate);
      } else {
        Serial.printf("Invalid header: %s\n", headerBuffer);
      }
    }
    headerIndex = 0;
  } else if (headerIndex < sizeof(headerBuffer) - 1) {
    headerBuffer[headerIndex++] = c;
  }
}

static void handleFooterByte(char c) {
  if (c == '\n') {
    footerBuffer[footerIndex] = '\0';
    if (strcmp(footerBuffer, "END") == 0) {
      Serial.println("Finished stream");
      playbackDonePending = true;
      pollSpeakerEvents();
    } else {
      Serial.printf("Unexpected footer: %s\n", footerBuffer);
    }
    footerIndex = 0;
    inboundState = WAITING_HEADER;
  } else if (footerIndex < sizeof(footerBuffer) - 1) {
    footerBuffer[footerIndex++] = c;
  }
}

static void pumpPcmToI2S() {
  static int16_t i2sBuffer[256];
  size_t queued = 0;

  pollSpeakerEvents();

  while (samplesRemaining > 0 && Serial.available() >= 2) {
    uint8_t raw[2];
    if (Serial.readBytes(raw, 2) != 2) break;

    int16_t s = int16_t(raw[0] | (raw[1] << 8)); // little endian
    i2sBuffer[queued++] = s;
    samplesRemaining--;

    if (queued == (sizeof(i2sBuffer) / sizeof(i2sBuffer[0]))) {
      size_t written = 0;
      i2s_write(I2S_PORT_SPK, i2sBuffer, queued * sizeof(int16_t), &written, portMAX_DELAY);
      spkSamplesInFlight += written / sizeof(int16_t);
      queued = 0;
    }
  }

  if (queued > 0) {
    size_t written = 0;
    i2s_write(I2S_PORT_SPK, i2sBuffer, queued * sizeof(int16_t), &written, portMAX_DELAY);
    spkSamplesInFlight += written / sizeof(int16_t);
  }

  if (samplesRemaining == 0 && inboundState == STREAMING_PCM) {
    inboundState = WAITING_FOOTER;
    Serial.println("Awaiting END footer");
  }

  pollSpeakerEvents();
}

static void handleInboundSerial() {
  // Single state machine for commands and playback stream
  while (Serial.available() > 0) {
    switch (inboundState) {
      case WAITING_HEADER:
        handleHeaderByte(static_cast<char>(Serial.read()));
        break;
      case STREAMING_PCM:
        pumpPcmToI2S();
        return; // give time to other tasks
      case WAITING_FOOTER:
        handleFooterByte(static_cast<char>(Serial.read()));
        break;
    }
  }
}

// ===== Arduino core =====
void setup() {
  Serial.setRxBufferSize(32768);
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_MIC_SEL, OUTPUT);
  digitalWrite(PIN_MIC_SEL, LOW); // select left channel on ICS-43434

  pinMode(PIN_AMP_SHUTDOWN, OUTPUT);
  digitalWrite(PIN_AMP_SHUTDOWN, HIGH); // enable amplifier

  setupI2SMicrophone();
  configureI2SSpeaker(spkSampleRateHz);
  playBootTone();

  sendLine("LOG duplex firmware booted");
  publishState();
  lastFlushMs = millis();
}

void loop() {
  pollSpeakerEvents();
  // 1) Service inbound speaker stream and commands first to avoid RX overflow
  handleInboundSerial();

  // 2) Capture mic frames and push to host
  static int32_t micBuffer[256];
  size_t bytesRead = 0;
  esp_err_t err = i2s_read(I2S_PORT_MIC, micBuffer, sizeof(micBuffer), &bytesRead, 10 / portTICK_PERIOD_MS);
  if (err == ESP_OK && bytesRead > 0) {
    size_t frames = bytesRead / sizeof(int32_t);
    processMicFrames(micBuffer, frames);
  } else {
    // small yield if no mic data
    delay(1);
  }

  // 3) Time-based flush of partial chunk to bound latency
  if (streamSamples > 0 && (millis() - lastFlushMs) >= FLUSH_MS) {
    sendAudioChunk();
  }
}
