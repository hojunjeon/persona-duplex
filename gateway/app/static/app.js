const $ = (id) => document.getElementById(id);
const DEFAULT_REFERENCE_MAX_BYTES = 32 * 1024 * 1024;
const REFERENCE_EXTENSIONS = new Set([".webm", ".wav", ".mp4", ".m4a", ".ogg", ".opus"]);
const state = {
  config: null,
  personas: [],
  selectedPersona: "",
  createdPersonaId: "",
  creatingPersona: false,
  activePersonaId: "",
  voices: [],
  selectedVoice: "",
  recordingFile: null,
  recordingFilename: "",
  recordingObjectUrl: "",
  uploadFile: null,
  uploadFilename: "",
  uploadObjectUrl: "",
  referenceMaxBytes: DEFAULT_REFERENCE_MAX_BYTES,
  referenceStream: null,
  recorder: null,
  recordStartedAt: 0,
  recordTimer: null,
  recordingSubmitting: false,
  uploadSubmitting: false,
  voiceMutations: new Set(),
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

function referenceLimitLabel(bytes = state.referenceMaxBytes) {
  const mebibyte = 1024 * 1024;
  return bytes % mebibyte === 0 ? `${bytes / mebibyte} MiB` : `${bytes.toLocaleString()}바이트`;
}

function updateReferenceUploadLimit() {
  const element = $("referenceUploadLimit");
  if (element) element.textContent = `파일 크기 제한: ${referenceLimitLabel()} 이하`;
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

function setPersonaControlsDisabled(disabled) {
  $("personaSelect").disabled = disabled;
  $("createPersona").disabled = disabled || state.creatingPersona;
  $("selectCreatedPersona").disabled = disabled || !state.createdPersonaId;
  $("personaCreatePanel").classList.toggle("disabled", disabled);
}

function renderPersonas(preferredId = "") {
  const select = $("personaSelect");
  const current = preferredId || state.selectedPersona || state.config?.default_persona || state.personas[0]?.id || "";
  select.innerHTML = "";
  for (const persona of state.personas) {
    const option = document.createElement("option");
    option.value = persona.id;
    option.textContent = `${persona.name} · ${persona.identity}`;
    option.selected = persona.id === current;
    select.appendChild(option);
  }
  state.selectedPersona = state.personas.some((persona) => persona.id === current)
    ? current
    : (state.personas[0]?.id || "");
  select.value = state.selectedPersona;
}

async function refreshPersonas(preferredId = "") {
  state.personas = await api("/api/personas");
  renderPersonas(preferredId);
}

async function loadConfig() {
  state.config = await api("/api/config");
  const configuredMax = Number(state.config.voice_max_upload_bytes);
  state.referenceMaxBytes = Number.isSafeInteger(configuredMax) && configuredMax > 0
    ? configuredMax
    : DEFAULT_REFERENCE_MAX_BYTES;
  updateReferenceUploadLimit();
  $("runtimeInfo").textContent = `${state.config.stt_mode} · ${state.config.tts_mode} · ${state.config.llm_model}`;
  await refreshPersonas(state.config.default_persona);
}

function linesToArray(id) {
  return $(id).value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function setPersonaCreateMessage(text, isError = false) {
  const element = $("personaCreateMessage");
  element.textContent = text;
  element.className = isError ? "small error" : "small";
}

function selectPersona(id) {
  if (state.ws) return;
  const persona = state.personas.find((item) => item.id === id);
  if (!persona) throw new Error("선택할 페르소나를 찾지 못했습니다. 목록을 새로고침하세요.");
  state.selectedPersona = persona.id;
  $("personaSelect").value = persona.id;
  if (state.createdPersonaId === persona.id) {
    state.createdPersonaId = "";
    $("selectCreatedPersona").hidden = true;
    $("selectCreatedPersona").disabled = true;
  }
  setPersonaCreateMessage(`선택 완료: ${persona.name}. 대화를 시작하려면 대화 시작을 누르세요.`);
}

async function submitPersona() {
  if (state.creatingPersona || state.ws) return;
  const name = $("personaName").value.trim();
  const identity = $("personaIdentity").value.trim();
  const relationship = $("personaRelationship").value.trim();
  if (!name || !identity || !relationship) throw new Error("이름, 정체성, 관계를 모두 입력하세요.");
  const maxSentences = Number($("personaMaxSentences").value);
  if (!Number.isInteger(maxSentences) || maxSentences < 1 || maxSentences > 8) {
    throw new Error("기본 문장 수는 1~8 사이여야 합니다.");
  }
  const payload = {
    name,
    identity,
    relationship,
    speaking_style: linesToArray("personaSpeakingStyle"),
    behavior: linesToArray("personaBehavior"),
    boundaries: linesToArray("personaBoundaries"),
    backchannels: linesToArray("personaBackchannels"),
    max_sentences: maxSentences,
  };
  const previousPersonaId = state.selectedPersona;
  state.creatingPersona = true;
  setPersonaControlsDisabled(true);
  setPersonaCreateMessage("페르소나 저장 중…");
  try {
    const response = await api("/api/personas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await refreshPersonas(previousPersonaId);
    const createdPersona = response.persona || {};
    state.createdPersonaId = createdPersona.id || "";
    const selectButton = $("selectCreatedPersona");
    selectButton.hidden = !state.createdPersonaId;
    selectButton.disabled = !state.createdPersonaId;
    setPersonaCreateMessage(`생성 완료: ${createdPersona.name || name}. 현재 선택은 유지했습니다. 새 페르소나를 사용하려면 옆의 선택 버튼을 누르세요.`);
  } finally {
    state.creatingPersona = false;
    setPersonaControlsDisabled(Boolean(state.ws));
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
    useButton.type = "button";
    useButton.setAttribute("aria-label", `${voice.display_name || id} 프로필 선택`);
    useButton.textContent = "선택";
    useButton.onclick = () => selectVoice(id);
    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.setAttribute("aria-label", `${voice.display_name || id} 프로필 편집`);
    editButton.textContent = "편집";
    editButton.onclick = () => editVoice(id).catch((error) => setMessage(error.message, true));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.setAttribute("aria-label", `${voice.display_name || id} 프로필 삭제`);
    deleteButton.textContent = "삭제";
    deleteButton.className = "danger";
    deleteButton.onclick = () => deleteVoice(id).catch((error) => setMessage(error.message, true));
    const mutationDisabled = state.voiceMutations.has(id);
    useButton.disabled = mutationDisabled;
    editButton.disabled = mutationDisabled;
    deleteButton.disabled = mutationDisabled;
    const actions = document.createElement("div");
    actions.className = "voice-card-actions";
    actions.append(useButton, editButton, deleteButton);
    card.append(label, actions);
    card.dataset.profileId = id;
    $("voiceList").appendChild(card);

    const option = document.createElement("option");
    option.value = id;
    option.textContent = voice.display_name || id;
    $("voiceSelect").appendChild(option);
  }
  const preferred = selectId || state.selectedVoice || "";
  const target = state.voices.some((voice) => voice.profile_id === preferred)
    ? preferred
    : (state.voices[0]?.profile_id || "");
  selectVoice(target);
}

function selectVoice(id) {
  state.selectedVoice = id || "";
  $("voiceSelect").value = state.selectedVoice;
  for (const card of $("voiceList").children) {
    card.classList.toggle("selected", card.dataset.profileId === state.selectedVoice);
  }
}

function setVoiceCardMutationUi(id, disabled) {
  for (const card of $("voiceList").children) {
    if (card.dataset.profileId !== id) continue;
    card.querySelectorAll("button").forEach((button) => { button.disabled = disabled; });
  }
}

async function editVoice(id) {
  if (state.ws) throw new Error("대화 중에는 목소리 프로필을 수정할 수 없습니다.");
  if (state.voiceMutations.has(id)) return;
  const profile = state.voices.find((voice) => voice.profile_id === id);
  if (!profile) throw new Error("목소리 프로필을 찾을 수 없습니다.");
  const displayName = window.prompt("프로필 이름", profile.display_name || "내 목소리");
  if (displayName === null) return;
  const transcript = window.prompt("참조 대본", profile.transcript || "");
  if (transcript === null) return;
  state.voiceMutations.add(id);
  setVoiceCardMutationUi(id, true);
  try {
    const response = await api(`/api/voices/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: displayName, transcript }),
    });
    setMessage("프로필 수정 완료. 모델 warm-up 중…");
    await api(`/api/voices/${encodeURIComponent(id)}/warmup`, { method: "POST" });
    state.voiceMutations.delete(id);
    await refreshVoices(id);
    setMessage(`프로필 수정 및 warm-up 완료: ${response.display_name || displayName}`);
  } finally {
    state.voiceMutations.delete(id);
    setVoiceCardMutationUi(id, false);
  }
}

async function deleteVoice(id) {
  if (state.ws) throw new Error("대화 중에는 목소리 프로필을 삭제할 수 없습니다.");
  if (state.voiceMutations.has(id)) return;
  const profile = state.voices.find((voice) => voice.profile_id === id);
  if (!profile) throw new Error("목소리 프로필을 찾을 수 없습니다.");
  if (!window.confirm(`\"${profile.display_name || id}\" 프로필을 삭제할까요? 이 작업은 되돌릴 수 없습니다.`)) return;
  const wasSelected = state.selectedVoice === id;
  state.voiceMutations.add(id);
  setVoiceCardMutationUi(id, true);
  try {
    await api(`/api/voices/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (wasSelected) state.selectedVoice = "";
    await refreshVoices();
    setMessage(`프로필 삭제 완료: ${profile.display_name || id}`);
  } finally {
    state.voiceMutations.delete(id);
    setVoiceCardMutationUi(id, false);
  }
}

function referenceExtension(filename) {
  const match = /\.([^.\\/]+)$/.exec(String(filename || ""));
  return match ? `.${match[1].toLowerCase()}` : "";
}

function referenceExtensionForMime(mimeType) {
  const mime = String(mimeType || "").toLowerCase().split(";", 1)[0];
  const extensions = {
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".mp4",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
  };
  return extensions[mime] || ".webm";
}

const VOICE_SOURCE_CONFIG = {
  recording: {
    fileKey: "recordingFile",
    filenameKey: "recordingFilename",
    objectUrlKey: "recordingObjectUrl",
    previewId: "recordingPreview",
    statusId: "recordingSource",
    messageId: "recordingMessage",
    submitId: "enrollRecordedVoice",
    label: "녹음",
  },
  upload: {
    fileKey: "uploadFile",
    filenameKey: "uploadFilename",
    objectUrlKey: "uploadObjectUrl",
    previewId: "uploadPreview",
    statusId: "uploadSource",
    messageId: "uploadMessage",
    submitId: "enrollUploadedVoice",
    label: "첨부 파일",
  },
};

function stopReferenceStream() {
  state.referenceStream?.getTracks().forEach((track) => track.stop());
  state.referenceStream = null;
}

function setSourceMessage(source, text, isError = false) {
  const config = VOICE_SOURCE_CONFIG[source];
  const element = $(config.messageId);
  element.textContent = text;
  element.className = isError ? "small error voice-source-message" : "small voice-source-message";
}

function clearAudioSource(source) {
  const config = VOICE_SOURCE_CONFIG[source];
  const objectUrl = state[config.objectUrlKey];
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  state[config.fileKey] = null;
  state[config.filenameKey] = "";
  state[config.objectUrlKey] = "";
  const preview = $(config.previewId);
  preview.removeAttribute("src");
  preview.load();
  $(config.statusId).textContent = "";
  $(config.submitId).disabled = true;
  setSourceMessage(source, "");
}

function setReferenceAudio(file, source = "upload") {
  if (!file || typeof file.size !== "number") throw new Error("기준 음성 파일을 읽을 수 없습니다.");
  const kind = source === "recording" ? "recording" : "upload";
  const config = VOICE_SOURCE_CONFIG[kind];
  const filename = String(file.name || "").split(/[\\/]/).pop() || "reference.webm";
  const extension = referenceExtension(filename);
  if (!REFERENCE_EXTENSIONS.has(extension)) {
    throw new Error("지원하지 않는 기준 음성 형식입니다. webm, wav, mp4, m4a, ogg, opus만 사용할 수 있습니다. mp3, flac, aac은 현재 서버에서 지원하지 않습니다.");
  }
  if (file.size <= 0) throw new Error("빈 기준 음성 파일입니다.");
  if (file.size > state.referenceMaxBytes) {
    throw new Error(`기준 음성 파일은 ${referenceLimitLabel()} 이하이어야 합니다.`);
  }

  const objectUrl = URL.createObjectURL(file);
  if (state[config.objectUrlKey]) URL.revokeObjectURL(state[config.objectUrlKey]);
  state[config.fileKey] = file;
  state[config.filenameKey] = filename;
  state[config.objectUrlKey] = objectUrl;
  $(config.previewId).src = objectUrl;
  $(config.statusId).textContent = `${config.label}: ${filename} (${(file.size / 1024).toFixed(1)} KiB)`;
  $(config.submitId).disabled = false;
}

function setReferenceRecordingUi(recording) {
  const active = recording || state.recorder?.state === "recording";
  $("recordReference").disabled = active || state.recordingSubmitting;
  $("stopReference").disabled = !active;
  $("enrollRecordedVoice").disabled = active || state.recordingSubmitting || !state.recordingFile;
}

function handleReferenceUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    setReferenceAudio(file, "upload");
    setSourceMessage("upload", "첨부 파일이 준비됐습니다. 대본을 실제 발화와 맞춘 뒤 이 방식의 등록 버튼을 누르세요.");
    setMessage("기준 음성 첨부 파일을 선택했습니다. 녹음 흐름과 별도로 첨부 파일 등록 버튼을 사용하세요.");
  } catch (error) {
    event.target.value = "";
    clearAudioSource("upload");
    setSourceMessage("upload", error.message, true);
    setMessage(error.message, true);
  }
}

async function startReferenceRecording() {
  if (state.recordingSubmitting) throw new Error("녹음 등록이 끝날 때까지 새 녹음을 시작할 수 없습니다.");
  if (state.recorder?.state === "recording") return;
  stopReferenceStream();
  state.referenceStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 }
  });
  clearAudioSource("recording");
  const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  const mimeType = mimeTypes.find((x) => MediaRecorder.isTypeSupported(x)) || "";
  const chunks = [];
  let recorder;
  try {
    recorder = new MediaRecorder(state.referenceStream, mimeType ? { mimeType } : undefined);
    recorder.start(200);
  } catch (error) {
    stopReferenceStream();
    setReferenceRecordingUi(false);
    throw error;
  }
  state.recorder = recorder;
  recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
  recorder.onstop = () => {
    const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    const filename = `reference${referenceExtensionForMime(recorder.mimeType || blob.type)}`;
    const file = typeof File === "function"
      ? new File([blob], filename, { type: blob.type || "audio/webm" })
      : blob;
    try {
      setReferenceAudio(file, "recording");
      setSourceMessage("recording", `녹음이 준비됐습니다. ${state.recordingFilename}을 등록하려면 녹음으로 프로필 생성을 누르세요.`);
    } catch (error) {
      clearAudioSource("recording");
      setSourceMessage("recording", error.message, true);
      setMessage(error.message, true);
    }
    stopReferenceStream();
    state.recorder = null;
    clearInterval(state.recordTimer);
    state.recordTimer = null;
    setReferenceRecordingUi(false);
    if (state.recordingFile) setMessage(`녹음 완료: ${state.recordingFilename} (${(state.recordingFile.size / 1024).toFixed(1)} KiB)`);
  };
  state.recordStartedAt = performance.now();
  state.recordTimer = setInterval(() => {
    const seconds = (performance.now() - state.recordStartedAt) / 1000;
    const min = String(Math.floor(seconds / 60)).padStart(2, "0");
    const sec = (seconds % 60).toFixed(1).padStart(4, "0");
    $("recordTimer").textContent = `${min}:${sec}`;
  }, 100);
  setReferenceRecordingUi(true);
  setMessage("녹음 중. 화면의 문장을 그대로 읽으세요.");
}

function stopReferenceRecording() {
  if (state.recorder?.state === "recording") state.recorder.stop();
}

function setVoiceSubmitUi(source, submitting) {
  const config = VOICE_SOURCE_CONFIG[source];
  $(config.submitId).disabled = submitting || !state[config.fileKey]
    || (source === "recording" && state.recorder?.state === "recording");
  if (source === "upload") $("referenceUpload").disabled = submitting;
  $(config.submitId).textContent = submitting
    ? `${config.label} 등록 중…`
    : `${config.label}으로 프로필 생성`;
}

async function enrollVoice(source) {
  const config = VOICE_SOURCE_CONFIG[source];
  if (!config) throw new Error("알 수 없는 목소리 등록 방식입니다.");
  if (state[`${source}Submitting`]) return;
  const file = state[config.fileKey];
  if (!file) throw new Error(`${config.label} 기준 음성을 먼저 준비하세요.`);
  if (!$("consent").checked) throw new Error("본인 목소리 또는 허가받은 목소리라는 동의가 필요합니다.");
  const transcript = $("referenceText").value.trim();
  if (transcript.length < 5 || transcript.length > 1000) throw new Error("참조 대본은 5~1000자여야 합니다.");
  const displayName = $("voiceName").value.trim() || "내 목소리";
  if (displayName.length > 80) throw new Error("프로필 이름은 80자 이하여야 합니다.");
  const form = new FormData();
  // Keep the browser's actual File (including its selected filename and MIME).
  form.append("audio", file, state[config.filenameKey] || file.name || "reference.webm");
  form.append("transcript", transcript);
  form.append("display_name", displayName);
  form.append("consent", "true");
  state[`${source}Submitting`] = true;
  setVoiceSubmitUi(source, true);
  if (source === "recording") setReferenceRecordingUi(false);
  try {
    setSourceMessage(source, `${config.label}으로 프로필 생성 중. 첫 등록은 모델 준비 때문에 오래 걸릴 수 있습니다.`);
    setMessage(`${config.label}으로 프로필 생성 중. 첫 등록은 모델 준비 때문에 오래 걸릴 수 있습니다.`);
    const profile = await api("/api/voices/enroll", { method: "POST", body: form });
    const qualityWarnings = profile.audio_quality?.warnings || [];
    const qualityText = qualityWarnings.length ? ` 녹음 경고: ${qualityWarnings.join(" ")}` : "";
    setSourceMessage(source, `등록 완료: ${profile.display_name} (${Number(profile.seconds || 0).toFixed(1)}초). 모델 warm-up 중…${qualityText}`);
    setMessage(`등록 완료: ${profile.display_name} (${Number(profile.seconds || 0).toFixed(1)}초). 모델 warm-up 중…${qualityText}`);
    await api(`/api/voices/${encodeURIComponent(profile.profile_id)}/warmup`, { method: "POST" });
    await refreshVoices(profile.profile_id);
    setSourceMessage(source, `등록·warm-up 완료: ${profile.display_name}. 이제 대화를 시작할 수 있습니다.${qualityText}`);
    setMessage(`목소리 프롬프트 준비 완료. 이제 대화를 시작할 수 있습니다.${qualityText}`);
  } finally {
    state[`${source}Submitting`] = false;
    setVoiceSubmitUi(source, false);
    if (source === "recording") setReferenceRecordingUi(false);
  }
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
  const personaId = state.selectedPersona || $("personaSelect").value;
  if (!personaId || !state.personas.some((persona) => persona.id === personaId)) {
    throw new Error("사용할 페르소나를 선택하세요.");
  }
  const profileId = $("voiceSelect").value;
  if (!profileId) throw new Error("사용할 목소리 프로필을 선택하세요.");
  state.activePersonaId = personaId;
  await player.ensure();
  setPersonaControlsDisabled(true);
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  try {
    state.ws = new WebSocket(`${protocol}//${location.host}/ws/conversation`);
  } catch (error) {
    state.activePersonaId = "";
    setPersonaControlsDisabled(false);
    throw error;
  }
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
    state.activePersonaId = "";
    setPersonaControlsDisabled(false);
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
        persona_id: state.activePersonaId || state.selectedPersona,
        voice_profile_id: $("voiceSelect").value,
      });
      break;
    case "session.configured":
      state.selectedPersona = message.persona || state.selectedPersona;
      $("personaSelect").value = state.selectedPersona;
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
  $("referenceUpload").onchange = handleReferenceUpload;
  $("enrollRecordedVoice").onclick = () => enrollVoice("recording").catch((e) => {
    setSourceMessage("recording", e.message, true);
    setMessage(e.message, true);
  });
  $("enrollUploadedVoice").onclick = () => enrollVoice("upload").catch((e) => {
    setSourceMessage("upload", e.message, true);
    setMessage(e.message, true);
  });
  $("refreshVoices").onclick = () => refreshVoices().catch((e) => setMessage(e.message, true));
  $("voiceSelect").onchange = () => selectVoice($("voiceSelect").value);
  $("personaSelect").onchange = () => {
    if (state.ws) {
      $("personaSelect").value = state.selectedPersona;
      return;
    }
    state.selectedPersona = $("personaSelect").value;
  };
  $("createPersona").onclick = () => submitPersona().catch((e) => setPersonaCreateMessage(e.message, true));
  $("selectCreatedPersona").onclick = () => {
    try {
      selectPersona(state.createdPersonaId);
    } catch (error) {
      setPersonaCreateMessage(error.message, true);
    }
  };
  $("startConversation").onclick = () => startConversation().catch((e) => appendBubble("system", e.message));
  $("stopConversation").onclick = stopConversation;
  window.addEventListener("beforeunload", () => {
    state.ws?.close();
    try { state.recorder?.stop(); } catch (_) {}
    stopReferenceStream();
    if (state.recordTimer) clearInterval(state.recordTimer);
    if (state.recordingObjectUrl) URL.revokeObjectURL(state.recordingObjectUrl);
    if (state.uploadObjectUrl) URL.revokeObjectURL(state.uploadObjectUrl);
  });
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
