// Antigravity Live Voice Client (Telegram Mini App)
(function() {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor('#0a0d14');
      tg.setBackgroundColor('#0a0d14');
      tg.enableClosingConfirmation();
    } catch(e) {}
  }

  // DOM Elements
  const statusPill = document.getElementById('statusPill');
  const statusText = document.getElementById('statusText');
  const activityState = document.getElementById('activityState');
  const orbCore = document.getElementById('orbCore');
  const orbRing = document.getElementById('orbRing');
  const userTranscript = document.getElementById('userTranscript');
  const userText = document.getElementById('userText');
  const aiTranscript = document.getElementById('aiTranscript');
  const aiText = document.getElementById('aiText');
  const micBtn = document.getElementById('micBtn');
  const speakerBtn = document.getElementById('speakerBtn');
  const dispatchBtn = document.getElementById('dispatchBtn');
  const endCallBtn = document.getElementById('endCallBtn');

  // State
  let ws = null;
  let inputAudioContext = null;
  let outputAudioContext = null;
  let micStream = null;
  let audioProcessor = null;
  let isMicMuted = false;
  let isSpeakerMuted = false;
  let nextPlayTime = 0;
  let activeSources = [];
  let isAiSpeaking = false;
  let latestUserPrompt = "";
  let heartbeatTimer = null;
  let isManualClose = false;
  let reconnectAttempts = 0;

  // Connect to WebSocket
  function initWebSocket() {
    if (isManualClose) return;

    const loc = window.location;
    const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${loc.host}/ws/live`;

    statusPill.className = 'status-pill status-connecting';
    statusText.textContent = reconnectAttempts > 0 ? 'Переподключение...' : 'Подключение...';
    activityState.textContent = 'Соединение с сервером Gemini Live...';

    if (ws) {
      try { ws.close(); } catch(e) {}
    }

    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = async () => {
      reconnectAttempts = 0;
      statusPill.className = 'status-pill status-live';
      statusText.textContent = 'В эфире';
      activityState.textContent = 'Слушаю вас... Говорите';
      if (tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');

      startHeartbeat();
      await startAudioPipeline();
    };

    ws.onmessage = async (event) => {
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          handleServerJson(msg);
        } catch (e) {
          console.error("JSON parse error:", e);
        }
      } else if (event.data instanceof ArrayBuffer) {
        if (!isSpeakerMuted) {
          playAudioChunk(event.data);
        }
      }
    };

    ws.onerror = (e) => {
      console.error("WS error:", e);
    };

    ws.onclose = () => {
      stopHeartbeat();
      stopAudioPipeline();

      if (!isManualClose) {
        statusPill.className = 'status-pill status-connecting';
        statusText.textContent = 'Обрыв связи';
        activityState.textContent = 'Связь прервалась. Восстановление...';
        
        // Auto reconnect with backoff
        reconnectAttempts++;
        const delay = Math.min(3000, 1000 * reconnectAttempts);
        setTimeout(() => {
          if (!isManualClose) {
            initWebSocket();
          }
        }, delay);
      } else {
        statusPill.className = 'status-pill status-connecting';
        statusText.textContent = 'Отключено';
        activityState.textContent = 'Разговор завершен';
      }
    };
  }

  function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch(e) {}
      }
    }, 10000);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function handleServerJson(msg) {
    if (msg.type === 'transcript') {
      if (msg.role === 'user') {
        userTranscript.classList.remove('empty');
        userText.textContent = msg.text;
        latestUserPrompt = msg.text;
      } else if (msg.role === 'assistant') {
        aiTranscript.classList.remove('empty');
        aiText.textContent = msg.text;
      }
    } else if (msg.type === 'state') {
      if (msg.state === 'speaking') {
        isAiSpeaking = true;
        activityState.textContent = 'Antigravity отвечает...';
        orbCore.style.transform = 'scale(1.15)';
      } else if (msg.state === 'listening') {
        isAiSpeaking = false;
        activityState.textContent = 'Слушаю вас...';
        orbCore.style.transform = 'scale(1.0)';
      }
    } else if (msg.type === 'interrupted') {
      // User started talking while AI was speaking: kill audio immediately
      clearAudioQueue();
      isAiSpeaking = false;
      activityState.textContent = 'Слушаю вас...';
      orbCore.style.transform = 'scale(1.0)';
    } else if (msg.type === 'task_dispatched') {
      activityState.textContent = '💻 Задача отправлена в Antigravity!';
      if (tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      setTimeout(() => {
        if (!isAiSpeaking) activityState.textContent = 'Слушаю вас...';
      }, 3000);
    }
  }

  // Audio Pipeline
  async function startAudioPipeline() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!outputAudioContext || outputAudioContext.state === 'closed') {
        outputAudioContext = new AudioCtx({ sampleRate: 24000 });
      }
      if (outputAudioContext.state === 'suspended') {
        await outputAudioContext.resume();
      }
      nextPlayTime = outputAudioContext.currentTime;

      if (!micStream) {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          }
        });
      }

      if (!inputAudioContext || inputAudioContext.state === 'closed') {
        inputAudioContext = new AudioCtx();
      }
      const inputSampleRate = inputAudioContext.sampleRate;
      const source = inputAudioContext.createMediaStreamSource(micStream);

      // 4096 samples buffer (~90ms) - reduces packet overhead by 50%
      const bufferSize = 4096;
      audioProcessor = inputAudioContext.createScriptProcessor(bufferSize, 1, 1);

      audioProcessor.onaudioprocess = (e) => {
        if (isMicMuted || !ws || ws.readyState !== WebSocket.OPEN) return;

        const inputData = e.inputBuffer.getChannelData(0);

        // Visualizer amplitude
        let sum = 0;
        for (let i = 0; i < inputData.length; i++) {
          sum += inputData[i] * inputData[i];
        }
        const rms = Math.sqrt(sum / inputData.length);
        if (!isAiSpeaking && rms > 0.02) {
          const scale = Math.min(1.3, 1.0 + rms * 2.5);
          orbCore.style.transform = `scale(${scale})`;
          orbRing.style.borderColor = `rgba(0, 210, 255, ${Math.min(1, 0.4 + rms * 3)})`;
        }

        // Downsample to 16000 Hz PCM
        const downsampled = downsampleBuffer(inputData, inputSampleRate, 16000);
        const pcm16 = floatTo16BitPCM(downsampled);

        ws.send(pcm16);
      };

      source.connect(audioProcessor);
      audioProcessor.connect(inputAudioContext.destination);

    } catch (err) {
      console.error("Microphone access error:", err);
      activityState.textContent = 'Доступ к микрофону отклонен';
      statusPill.className = 'status-pill status-error';
      statusText.textContent = 'Нет микрофона';
    }
  }

  function stopAudioPipeline() {
    if (audioProcessor) {
      audioProcessor.disconnect();
      audioProcessor = null;
    }
    clearAudioQueue();
  }

  // Playback PCM 24kHz Little-Endian
  function playAudioChunk(arrayBuffer) {
    if (!outputAudioContext || outputAudioContext.state === 'closed') return;

    if (outputAudioContext.state === 'suspended') {
      outputAudioContext.resume();
    }

    const int16Array = new Int16Array(arrayBuffer);
    const float32Array = new Float32Array(int16Array.length);

    let sum = 0;
    for (let i = 0; i < int16Array.length; i++) {
      const f = int16Array[i] / 32768.0;
      float32Array[i] = f;
      sum += f * f;
    }

    const rms = Math.sqrt(sum / int16Array.length);
    if (rms > 0.01) {
      const scale = Math.min(1.35, 1.05 + rms * 2.8);
      orbCore.style.transform = `scale(${scale})`;
      orbRing.style.borderColor = `rgba(157, 80, 187, ${Math.min(1, 0.4 + rms * 3)})`;
    }

    const audioBuffer = outputAudioContext.createBuffer(1, float32Array.length, 24000);
    audioBuffer.copyToChannel(float32Array, 0);

    const source = outputAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(outputAudioContext.destination);

    const currentTime = outputAudioContext.currentTime;
    if (nextPlayTime < currentTime) {
      nextPlayTime = currentTime;
    }

    source.start(nextPlayTime);
    nextPlayTime += audioBuffer.duration;

    activeSources.push(source);
    source.onended = () => {
      const idx = activeSources.indexOf(source);
      if (idx !== -1) activeSources.splice(idx, 1);
      if (activeSources.length === 0) {
        orbCore.style.transform = 'scale(1.0)';
      }
    };
  }

  function clearAudioQueue() {
    for (const src of activeSources) {
      try { src.stop(); } catch(e) {}
    }
    activeSources = [];
    if (outputAudioContext) {
      nextPlayTime = outputAudioContext.currentTime;
    }
  }

  // Audio Resampling and PCM helpers
  function downsampleBuffer(buffer, sampleRate, outSampleRate) {
    if (outSampleRate === sampleRate) {
      return buffer;
    }
    if (outSampleRate > sampleRate) {
      throw new Error("Downsampling rate must be smaller than original");
    }
    const sampleRateRatio = sampleRate / outSampleRate;
    const newLength = Math.round(buffer.length / sampleRateRatio);
    const result = new Float32Array(newLength);
    let offsetResult = 0;
    let offsetBuffer = 0;
    while (offsetResult < result.length) {
      const nextOffsetBuffer = Math.round((offsetResult + 1) * sampleRateRatio);
      let accum = 0, count = 0;
      for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
        accum += buffer[i];
        count++;
      }
      result[offsetResult] = count > 0 ? accum / count : 0;
      offsetResult++;
      offsetBuffer = nextOffsetBuffer;
    }
    return result;
  }

  function floatTo16BitPCM(input) {
    const output = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      output[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return output.buffer;
  }

  // UI Event Handlers
  micBtn.addEventListener('click', () => {
    isMicMuted = !isMicMuted;
    if (isMicMuted) {
      micBtn.classList.remove('active');
      micBtn.classList.add('muted');
      micBtn.querySelector('.btn-label').textContent = 'Выкл';
    } else {
      micBtn.classList.remove('muted');
      micBtn.classList.add('active');
      micBtn.querySelector('.btn-label').textContent = 'Микрофон';
    }
    if (tg?.HapticFeedback) tg.HapticFeedback.selectionChanged();
  });

  speakerBtn.addEventListener('click', () => {
    isSpeakerMuted = !isSpeakerMuted;
    if (isSpeakerMuted) {
      speakerBtn.classList.remove('active');
      speakerBtn.classList.add('muted');
      speakerBtn.querySelector('.btn-label').textContent = 'Без звука';
      clearAudioQueue();
    } else {
      speakerBtn.classList.remove('muted');
      speakerBtn.classList.add('active');
      speakerBtn.querySelector('.btn-label').textContent = 'Звук';
    }
    if (tg?.HapticFeedback) tg.HapticFeedback.selectionChanged();
  });

  dispatchBtn.addEventListener('click', async () => {
    const textToDispatch = latestUserPrompt || userText.textContent;
    if (!textToDispatch || textToDispatch === 'Говорите...') {
      activityState.textContent = 'Сначала скажите что-то в микрофон';
      return;
    }

    activityState.textContent = 'Отправка задачи в Antigravity...';
    try {
      const res = await fetch('/api/dispatch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: textToDispatch })
      });
      const data = await res.json();
      if (data.ok) {
        activityState.textContent = '✓ Задача выполняется в Antigravity!';
        if (tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      } else {
        activityState.textContent = 'Ошибка отправки: ' + (data.error || 'неизвестно');
      }
    } catch(e) {
      activityState.textContent = 'Сбой сети при отправке задачи';
    }
  });

  endCallBtn.addEventListener('click', () => {
    isManualClose = true;
    if (tg?.HapticFeedback) tg.HapticFeedback.impactOccurred('heavy');
    stopHeartbeat();
    stopAudioPipeline();
    if (micStream) {
      micStream.getTracks().forEach(t => t.stop());
      micStream = null;
    }
    if (inputAudioContext) {
      try { inputAudioContext.close(); } catch(e) {}
    }
    if (outputAudioContext) {
      try { outputAudioContext.close(); } catch(e) {}
    }
    if (ws) {
      try { ws.close(); } catch(e) {}
    }
    if (tg) {
      tg.close();
    } else {
      activityState.textContent = 'Звонок завершен. Можно закрыть окно.';
    }
  });

  // Start on load
  window.addEventListener('DOMContentLoaded', () => {
    initWebSocket();
  });
})();
