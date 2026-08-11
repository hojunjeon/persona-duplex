const $ = (id) => document.getElementById(id);
const state = {
  config: null,
  voices: [],
  selectedVoice: "",
  referenceBlob: null,
  referenceStream: null,
  recorder: null,
  recordStartedAt: 0,
  recordTimer: null,
  ws: null,
  micStream: null,
  micContext: null,
  micNode: null,
  micSource: null,
  speechActive: false,
  aboveFrames: 0,
  belowFrames: 0,
  noiseFloor: 0.006,
  assistantPlaying: false,
  currentAssistantTurn: null,
  assistantBubbles: new Map(),
  metrics: { stt: null, llm: null, audio: null },
};

function setMessage(text, isError = false) {
  $("enrollMessage").textContent = text;
  $("enrollMessage").className = isError ? "small error" : "small";
}

function setStatus(text, live = false) {
  $("statusText").textContent = text;
  $("statusDot").classList.toggle("live", live);
}

function appendBubble(role, text, id = null) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  if (id) bubble.dataset.turnId = id;
  $("chatLog").appendChild(bubble);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
  return bubble;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      message = data.detail || data.message || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

class DuplexPlayer {
  constructor(onPlayed, onIdle) {
    this.context = null;
    this.mainGain = null;
    this.backGain = null;
    this.nextTime = { 1: 0, 2: 0 };
    this.sources = { 1: new Set(), 2: new Set() };
    this.streams = new Map();
    this.onPlayed = onPlayed;
    this.onIdle = onIdle;
    this.generationDone = true;
    this.idleSent = true;
    this.generationTurnId = null;
  }

  async ensure() {
    if (!this.context) {
      this.context = new AudioContext({ latencyHint: "interactive" });
      this.mainGain = this.context.createGain();
      this.backGain = this.context.createGain();
      this.mainGain.gain.value = 1;
      this.backGain.gain.value = 0.52;
      this.mainGain.connect(this.context.destination);
      this.backGain.connect(this.context.destination);
    }
    if (this.context.state !== "running") await this.context.resume();
  }

  beginGeneration(turnId) {
    this.generationDone = false;
    this.idleSent = false;
    this.generationTurnId = turnId || null;
  }

  markGenerationDone() {
    this.generationDone = true;
    this.checkIdle();
  }

  beginStream(streamId, channel) {
    if (!this.streams.has(streamId)) {
      this.streams.set(streamId, { channel, active: 0, ended: false, cancelled: false });
    }
  }

  endStream(streamId) {
    const stream = this.streams.get(streamId);
    if (!stream) return;
    stream.ended = true;
    this.finishStreamIfReady(streamId, stream);
  }

  async addFrame(arrayBuffer) {
    await this.ensure();
    if (arrayBuffer.byteLength < 9) return;
    const view = new DataView(arrayBuffer);
    const channel = view.getUint8(0);
    const streamId = view.getUint32(1, true);
    const sampleRate = view.getUint32(5, true);
    const sampleCount = Math.floor((arrayBuffer.byteLength - 9) / 2);
    if (!sampleCount || ![1, 2].includes(channel)) return;

    this.beginStream(streamId, channel);
    const stream = this.streams.get(streamId);
    const audioBuffer = this.context.createBuffer(1, sampleCount, sampleRate);
    const output = audioBuffer.getChannelData(0);
    let offset = 9;
    for (let i = 0; i < sampleCount; i++, offset += 2) {
      output[i] = view.getInt16(offset, true) / 32768;
    }

    const source = this.context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(channel === 1 ? this.mainGain : this.backGain);
    const startAt = Math.max(this.context.currentTime + 0.025, this.nextTime[channel] || 0);
    this.nextTime[channel] = startAt + audioBuffer.duration;
    stream.active += 1;
    this.sources[channel].add(source);
    source.onended = () => {
      this.sources[channel].delete(source);
      stream.active = Math.max(0, stream.active - 1);
      this.finishStreamIfReady(streamId, stream);
      this.checkIdle();
    };
    source.start(startAt);
  }

  finishStreamIfReady(streamId, stream) {
    if (!stream.ended || stream.active !== 0) return;
    this.streams.delete(streamId);
    if (!stream.cancelled && stream.channel === 1) this.onPlayed(streamId);
    this.checkIdle();
  }

  duck(factor = 0.18, attackMs = 45) {
    if (!this.context || !this.mainGain) return;
    const now = this.context.currentTime;
    this.mainGain.gain.cancelScheduledValues(now);
    this.mainGain.gain.setTargetAtTime(factor, now, Math.max(0.008, attackMs / 4000));
  }

  unduck(releaseMs = 100) {
    if (!this.context || !this.mainGain) return;
    const now = this.context.currentTime;
    this.mainGain.gain.cancelScheduledValues(now);
    this.mainGain.gain.setTargetAtTime(1, now, Math.max(0.008, releaseMs / 4000));
  }

  stopChannel(channel) {
    // Mark streams cancelled before source.stop(); onended may run immediately on
    // some browsers, and a cancelled half-clause must never be committed as heard.
    for (const [streamId, stream] of [...this.streams.entries()]) {
      if (stream.channel === channel) {
        stream.cancelled = true;
        stream.ended = true;
        stream.active = 0;
        this.streams.delete(streamId);
      }
    }
    const set = this.sources[channel];
    for (const source of [...set]) {
      try { source.stop(); } catch (_) {}
    }
    set.clear();
    if (this.context) this.nextTime[channel] = this.context.currentTime + 0.02;
    if (channel === 1) {
      this.generationDone = true;
      this.checkIdle();
    }
  }

  hasMainAudio() {
    if (this.sources[1].size) return true;
    for (const stream of this.streams.values()) if (stream.channel === 1) return true;
    return false;
  }

  checkIdle() {
    if (this.generationDone && !this.hasMainAudio() && !this.idleSent) {
      this.idleSent = true;
      const turnId = this.generationTurnId;
      this.generationTurnId = null;
      this.onIdle(turnId);
    }
  }
}

const player = new DuplexPlayer(
  (streamId) => sendControl({ type: "audio.played", stream_id: streamId }),
  (turnId) => {
    state.assistantPlaying = false;
    sendControl({ type: "assistant.playback_idle", turn_id: turnId });
  }
);

function sendControl(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
  }
}

async function loadConfig() {
  state.config = await api("/api/config");
  $("personaSelect").innerHTML = "";
  $("runtimeInfo").textContent = `${state.config.stt_mode} · ${state.config.tts_mode} · ${state.config.llm_model}`;
  for (const persona of state.config.personas) {
    const option = document.createElement("option");
    option.value = persona.id;
    option.textContent = `${persona.name} · ${persona.identity}`;
    option.selected = persona.id === state.config.default_persona;
    $("personaSelect").appendChild(option);
  }
}

async function refreshVoices(selectId = null) {
  state.voices = await api("/api/voices");
  $("voiceList").innerHTML = "";
  $("voiceSelect").innerHTML = "";
  for (const voice of state.voices) {
    const id = voice.profile_id;
    const card = document.createElement("div");
    card.className = "voice-card";
    const label = document.createElement("div");
    label.innerHTML = `<strong></strong><div class="small"></div>`;
    label.querySelector("strong").textContent = voice.display_name || id;
    label.querySelector(".small").textContent = `${Number(voice.seconds || 0).toFixed(1)}초 · ${id}`;
    const useButton = document.createElement("button");
    useButton.textContent = "선택";
    useButton.onclick = () => selectVoice(id);
    card.append(label, useButton);
    card.dataset.profileId = id;
    $("voiceList").appendChild(card);

    const option = document.createElement("option");
    option.value = id;
    option.textContent = voice.display_name || id;
    $("voiceSelect").appendChild(option);
  }
  const target = selectId || state.selectedVoice || state.voices[0]?.profile_id || "";
  selectVoice(target);
}

function selectVoice(id) {
  state.selectedVoice = id || "";
  $("voiceSelect").value = state.selectedVoice;
  for (const card of $("voiceList").children) {
    card.classList.toggle("selected", card.dataset.profileId === state.selectedVoice);
  }
}

async function startReferenceRecording() {
  state.referenceBlob = null;
  state.referenceStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 }
  });
  const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  const mimeType = mimeTypes.find((x) => MediaRecorder.isTypeSupported(x)) || "";
  const chunks = [];
  state.recorder = new MediaRecorder(state.referenceStream, mimeType ? { mimeType } : undefined);
  state.recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
  state.recorder.onstop = () => {
    state.referenceBlob = new Blob(chunks, { type: state.recorder.mimeType || "audio/webm" });
    $("referencePreview").src = URL.createObjectURL(state.referenceBlob);
    state.referenceStream?.getTracks().forEach((track) => track.stop());
    state.referenceStream = null;
    clearInterval(state.recordTimer);
    $("recordReference").disabled = false;
    $("stopReference").disabled = true;
    setMessage(`녹음 완료: ${(state.referenceBlob.size / 1024).toFixed(1)} KiB`);
  };
  state.recorder.start(200);
  state.recordStartedAt = performance.now();
  state.recordTimer = setInterval(() => {
    const seconds = (performance.now() - state.recordStartedAt) / 1000;
    const min = String(Math.floor(seconds / 60)).padStart(2, "0");
    const sec = (seconds % 60).toFixed(1).padStart(4, "0");
    $("recordTimer").textContent = `${min}:${sec}`;
  }, 100);
  $("recordReference").disabled = true;
  $("stopReference").disabled = false;
  setMessage("녹음 중. 화면의 문장을 그대로 읽으세요.");
}

function stopReferenceRecording() {
  if (state.recorder?.state === "recording") state.recorder.stop();
}

async function enrollVoice() {
  if (!state.referenceBlob) throw new Error("먼저 기준 음성을 녹음하세요.");
  if (!$("consent").checked) throw new Error("본인 목소리 또는 허가받은 목소리라는 동의가 필요합니다.");
  const transcript = $("referenceText").value.trim();
  if (!transcript) throw new Error("참조 대본이 비어 있습니다.");
  const form = new FormData();
  form.append("audio", state.referenceBlob, "reference.webm");
  form.append("transcript", transcript);
  form.append("display_name", $("voiceName").value.trim() || "내 목소리");
  form.append("consent", "true");
  setMessage("프로필 생성 중. 첫 등록은 모델 준비 때문에 오래 걸릴 수 있습니다.");
  const profile = await api("/api/voices/enroll", { method: "POST", body: form });
  const qualityWarnings = profile.audio_quality?.warnings || [];
  const qualityText = qualityWarnings.length ? ` 녹음 경고: ${qualityWarnings.join(" ")}` : "";
  setMessage(`등록 완료: ${profile.display_name} (${Number(profile.seconds || 0).toFixed(1)}초). 프롬프트를 준비합니다.${qualityText}`);
  await api(`/api/voices/${encodeURIComponent(profile.profile_id)}/warmup`, { method: "POST" });
  await refreshVoices(profile.profile_id);
  setMessage(`목소리 프롬프트 준비 완료. 이제 대화를 시작할 수 있습니다.${qualityText}`);
}

async function startMicCapture() {
  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      sampleRate: 48000,
      latency: 0.01,
    }
  });
  state.micContext = new AudioContext({ latencyHint: "interactive" });
  await state.micContext.audioWorklet.addModule("/static/pcm-worklet.js");
  state.micSource = state.micContext.createMediaStreamSource(state.micStream);
  state.micNode = new AudioWorkletNode(state.micContext, "pcm-capture", {
    processorOptions: { targetSampleRate: 16000 }
  });
  const mute = state.micContext.createGain();
  mute.gain.value = 0;
  state.micSource.connect(state.micNode);
  state.micNode.connect(mute).connect(state.micContext.destination);
  state.micNode.port.onmessage = ({ data }) => {
    if (data.type !== "pcm") return;
    processMicPacket(data.pcm, Number(data.rms || 0));
  };
  await state.micContext.resume();
}

function processMicPacket(pcm, rms) {
  $("micMeter").style.width = `${Math.min(100, rms * 900)}%`;
  if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(pcm);

  if (!state.speechActive) {
    state.noiseFloor = state.noiseFloor * 0.985 + Math.min(rms, 0.03) * 0.015;
  }
  const audioConfig = state.config?.audio || {};
  const thresholdMin = Number(audioConfig.vad_threshold_min || 0.012);
  const multiplier = state.assistantPlaying
    ? Number(audioConfig.vad_noise_multiplier_assistant || 3.4)
    : Number(audioConfig.vad_noise_multiplier_idle || 2.7);
  const threshold = Math.max(thresholdMin, state.noiseFloor * multiplier);
  if (rms > threshold) {
    state.aboveFrames += 1;
    state.belowFrames = 0;
  } else {
    state.belowFrames += 1;
    state.aboveFrames = 0;
  }

  if (!state.speechActive && state.aboveFrames >= Number(audioConfig.vad_start_frames || 3)) {
    state.speechActive = true;
    state.belowFrames = 0;
    // Duck locally before the round trip to the server. False positives recover
    // through audio.unduck, while real barge-in feels immediate.
    if (state.assistantPlaying || player.hasMainAudio()) player.duck(0.18, 28);
    sendControl({ type: "client.speech_start", rms, threshold });
  }
  // 20 ms packets: 300 ms while the bot is speaking, 520 ms otherwise.
  // The previous 160 ms cut Korean word-final consonants and short pauses too often.
  const endMs = state.assistantPlaying
    ? Number(audioConfig.vad_end_ms_assistant || 300)
    : Number(audioConfig.vad_end_ms_idle || 520);
  const endFrames = Math.max(1, Math.round(endMs / 20));
  if (state.speechActive && state.belowFrames >= endFrames) {
    state.speechActive = false;
    state.aboveFrames = 0;
    sendControl({ type: "client.speech_end" });
  }
}

async function stopMicCapture() {
  if (state.speechActive) {
    state.speechActive = false;
    sendControl({ type: "client.speech_end" });
  }
  try { state.micNode?.disconnect(); } catch (_) {}
  try { state.micSource?.disconnect(); } catch (_) {}
  state.micStream?.getTracks().forEach((track) => track.stop());
  if (state.micContext) await state.micContext.close();
  state.micStream = null;
  state.micContext = null;
  state.micNode = null;
  state.micSource = null;
  $("micMeter").style.width = "0%";
}

async function startConversation() {
  const profileId = $("voiceSelect").value;
  if (!profileId && state.config.tts_mode !== "mock") throw new Error("사용할 목소리 프로필을 선택하세요.");
  await player.ensure();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  state.ws = new WebSocket(`${protocol}//${location.host}/ws/conversation`);
  state.ws.binaryType = "arraybuffer";

  state.ws.onopen = async () => {
    try {
      await startMicCapture();
      $("startConversation").disabled = true;
      $("stopConversation").disabled = false;
      setStatus("연결 중", true);
    } catch (error) {
      appendBubble("system", `마이크 시작 실패: ${error.message}`);
      state.ws.close();
    }
  };
  state.ws.onmessage = async (event) => {
    if (event.data instanceof ArrayBuffer) {
      await player.addFrame(event.data);
      return;
    }
    const message = JSON.parse(event.data);
    handleServerMessage(message);
  };
  state.ws.onerror = () => appendBubble("system", "WebSocket 연결 오류가 발생했습니다.");
  state.ws.onclose = async () => {
    player.stopChannel(1);
    player.stopChannel(2);
    await stopMicCapture();
    state.ws = null;
    state.assistantPlaying = false;
    $("startConversation").disabled = false;
    $("stopConversation").disabled = true;
    setStatus("정지", false);
  };
}

function handleServerMessage(message) {
  switch (message.type) {
    case "session.ready":
      sendControl({
        type: "session.configure",
        persona_id: $("personaSelect").value,
        voice_profile_id: $("voiceSelect").value,
      });
      break;
    case "session.configured":
      appendBubble("system", `${message.persona_name} 페르소나와 목소리 프로필이 준비됐습니다.`);
      setStatus("듣는 중", true);
      break;
    case "session.state": {
      const labels = { idle: "듣는 중", listening: "말을 듣는 중", thinking: "생각 중", speaking: "말하는 중" };
      setStatus(labels[message.state] || message.state, true);
      state.assistantPlaying = Boolean(message.assistant_playing) || player.hasMainAudio();
      break;
    }
    case "transcript.partial":
      $("liveTranscript").textContent = message.text || "인식 중";
      break;
    case "transcript.final":
      $("liveTranscript").textContent = "인식 대기";
      appendBubble("user", message.text);
      break;
    case "transcript.empty":
      $("liveTranscript").textContent = "음성을 인식하지 못했습니다.";
      break;
    case "assistant.turn_start": {
      state.currentAssistantTurn = message.turn_id;
      player.beginGeneration(message.turn_id);
      const bubble = appendBubble("assistant", "", message.turn_id);
      state.assistantBubbles.set(message.turn_id, bubble);
      break;
    }
    case "assistant.delta": {
      const bubble = state.assistantBubbles.get(message.turn_id);
      if (bubble) {
        bubble.textContent += message.text;
        $("chatLog").scrollTop = $("chatLog").scrollHeight;
      }
      break;
    }
    case "audio.begin":
      player.beginStream(message.stream_id, message.channel);
      if (message.channel === 1) state.assistantPlaying = true;
      break;
    case "audio.end":
      player.endStream(message.stream_id);
      break;
    case "assistant.generation_done":
      player.markGenerationDone();
      break;
    case "audio.duck":
      player.duck(message.factor, message.attack_ms);
      break;
    case "audio.unduck":
      player.unduck(message.release_ms);
      break;
    case "audio.stop":
      player.stopChannel(message.channel || 1);
      state.assistantPlaying = false;
      break;
    case "assistant.interrupted":
      appendBubble("system", "사용자 발화로 이전 답변을 중단했습니다.");
      break;
    case "user.backchannel":
      player.unduck(90);
      break;
    case "metrics.stt_final":
      state.metrics.stt = message.milliseconds;
      updateMetrics();
      break;
    case "metrics.llm_first_token":
      state.metrics.llm = message.milliseconds;
      updateMetrics();
      break;
    case "metrics.first_audio":
      state.metrics.audio = message.milliseconds;
      updateMetrics();
      break;
    case "warning":
      appendBubble("system", `경고: ${message.message}`);
      break;
    case "error":
      appendBubble("system", `오류[${message.source || "server"}]: ${message.message}`);
      break;
    default:
      break;
  }
}

function updateMetrics() {
  const fmt = (value) => value == null ? "-" : `${Math.round(value)} ms`;
  $("metricStt").textContent = fmt(state.metrics.stt);
  $("metricLlm").textContent = fmt(state.metrics.llm);
  $("metricAudio").textContent = fmt(state.metrics.audio);
}

async function stopConversation() {
  sendControl({ type: "client.stop" });
  if (state.ws) state.ws.close(1000, "user stopped");
}

function bindEvents() {
  $("recordReference").onclick = () => startReferenceRecording().catch((e) => setMessage(e.message, true));
  $("stopReference").onclick = stopReferenceRecording;
  $("enrollVoice").onclick = () => enrollVoice().catch((e) => setMessage(e.message, true));
  $("refreshVoices").onclick = () => refreshVoices().catch((e) => setMessage(e.message, true));
  $("voiceSelect").onchange = () => selectVoice($("voiceSelect").value);
  $("startConversation").onclick = () => startConversation().catch((e) => appendBubble("system", e.message));
  $("stopConversation").onclick = stopConversation;
  window.addEventListener("beforeunload", () => state.ws?.close());
}

(async function boot() {
  try {
    bindEvents();
    await loadConfig();
    await refreshVoices();
  } catch (error) {
    appendBubble("system", `초기화 실패: ${error.message}`);
  }
})();
