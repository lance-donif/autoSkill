"use strict";

const voiceStart = document.getElementById("voice-start");
const voiceStop = document.getElementById("voice-stop");
const voiceSource = document.getElementById("voice-source");
const voiceDevice = document.getElementById("voice-device");
const voiceAudio = document.getElementById("voice-audio");
const voiceError = document.getElementById("voice-error");
let activeVoice = null;
let voiceState = {
  status: "idle", source: "microphone", permission: "unknown", sender: "new", receiver: "new",
  localAudioTracks: 0, remoteAudioTracks: 0, packetsReceived: 0, bytesReceived: 0, totalAudioEnergy: 0, peakRms: 0, cleanup: null,
};

function renderVoice() {
  const labels = { idle: "尚未开始", starting: "正在建立本地音频连接", connected: "音频回环已连接", stopped: "已结束并释放设备", error: "音频接入失败" };
  const permissions = { unknown: "尚未请求", requesting: "正在请求", granted: "已允许", denied: "未允许", unused: "合成音不需要麦克风" };
  document.getElementById("voice-status").textContent = labels[voiceState.status];
  document.getElementById("voice-permission").textContent = "麦克风权限：" + permissions[voiceState.permission];
  document.getElementById("voice-received").textContent = "接收音频：" + voiceState.packetsReceived + " 个包，" + voiceState.bytesReceived + " 字节，实测音量 " + voiceState.peakRms.toFixed(4);
  document.getElementById("voice-details").textContent = JSON.stringify(voiceState, null, 2);
  voiceStart.disabled = activeVoice !== null;
  voiceStop.disabled = activeVoice === null;
  voiceSource.disabled = activeVoice !== null;
  voiceDevice.disabled = activeVoice !== null || voiceSource.value === "synthetic";
}

async function refreshDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const selected = voiceDevice.value;
  const devices = await navigator.mediaDevices.enumerateDevices();
  voiceDevice.replaceChildren(new Option("系统默认麦克风", ""));
  for (const device of devices.filter((item) => item.kind === "audioinput")) {
    voiceDevice.add(new Option(device.label || "麦克风 " + (voiceDevice.options.length), device.deviceId));
  }
  voiceDevice.value = selected;
}

async function localAudio(session) {
  if (voiceSource.value === "synthetic") {
    const destination = session.context.createMediaStreamDestination();
    const oscillator = session.context.createOscillator();
    const volume = session.context.createGain();
    volume.gain.value = 0.1;
    oscillator.frequency.value = 440;
    oscillator.connect(volume).connect(destination);
    oscillator.start();
    session.oscillator = oscillator;
    voiceState.permission = "unused";
    return destination.stream;
  }
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前页面无法访问麦克风，请使用 localhost 或 HTTPS 打开");
  voiceState.permission = "requesting";
  renderVoice();
  const deviceId = voiceDevice.value;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { ...(deviceId ? { deviceId: { exact: deviceId } } : {}), echoCancellation: false, noiseSuppression: false }, video: false });
  voiceState.permission = "granted";
  await refreshDevices();
  return stream;
}

function connectionState(session) {
  if (activeVoice !== session) return;
  voiceState.sender = session.sender.connectionState;
  voiceState.receiver = session.receiver.connectionState;
  if (voiceState.sender === "connected" && voiceState.receiver === "connected") voiceState.status = "connected";
  renderVoice();
}

function remoteAudio(session, event) {
  if (activeVoice !== session) return;
  session.remote.addTrack(event.track);
  voiceAudio.srcObject = session.remote;
  voiceState.remoteAudioTracks = session.remote.getAudioTracks().length;
  const source = session.context.createMediaStreamSource(session.remote);
  const analyser = session.context.createAnalyser();
  const mute = session.context.createGain();
  mute.gain.value = 0;
  analyser.fftSize = 1024;
  source.connect(analyser).connect(mute).connect(session.context.destination);
  session.analyser = analyser;
  renderVoice();
}

async function relayCandidate(session, target, queue, event) {
  if (!event.candidate || activeVoice !== session) return;
  if (!target.remoteDescription) {
    queue.push(event.candidate);
    return;
  }
  await target.addIceCandidate(event.candidate);
}

function sessionError(session, error) {
  if (activeVoice === session) return showVoiceError(error);
  return undefined;
}

function gathered(session, connection) {
  if (activeVoice !== session || connection.iceGatheringState !== "complete") return;
  if (!connection.localDescription?.sdp.includes("a=candidate:")) {
    sessionError(session, new Error("浏览器没有提供可用的 WebRTC 网络候选，请检查媒体或网络策略"));
  }
}

async function negotiate(session) {
  const forSender = [];
  const forReceiver = [];
  session.sender.onicecandidate = (event) => relayCandidate(session, session.receiver, forReceiver, event).catch((error) => sessionError(session, error));
  session.receiver.onicecandidate = (event) => relayCandidate(session, session.sender, forSender, event).catch((error) => sessionError(session, error));
  session.sender.onicegatheringstatechange = () => gathered(session, session.sender);
  session.receiver.onicegatheringstatechange = () => gathered(session, session.receiver);
  session.sender.onconnectionstatechange = () => connectionState(session);
  session.receiver.onconnectionstatechange = () => connectionState(session);
  session.receiver.ontrack = (event) => remoteAudio(session, event);
  for (const track of session.local.getTracks()) session.sender.addTrack(track, session.local);
  await session.sender.setLocalDescription(await session.sender.createOffer());
  await session.receiver.setRemoteDescription(session.sender.localDescription);
  for (const candidate of forReceiver) await session.receiver.addIceCandidate(candidate);
  await session.receiver.setLocalDescription(await session.receiver.createAnswer());
  await session.sender.setRemoteDescription(session.receiver.localDescription);
  for (const candidate of forSender) await session.sender.addIceCandidate(candidate);
}

async function sampleVoice(session) {
  if (activeVoice !== session) return;
  const reports = await session.receiver.getStats();
  if (activeVoice !== session) return;
  for (const report of reports.values()) {
    if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
      voiceState.packetsReceived = report.packetsReceived || 0;
      voiceState.bytesReceived = report.bytesReceived || 0;
      voiceState.totalAudioEnergy = report.totalAudioEnergy || 0;
    }
  }
  if (session.analyser) {
    const samples = new Float32Array(session.analyser.fftSize);
    session.analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((total, value) => total + value * value, 0) / samples.length);
    voiceState.peakRms = Math.max(voiceState.peakRms, rms);
  }
  renderVoice();
}

async function stopVoice() {
  const session = activeVoice;
  activeVoice = null;
  if (session) {
    clearInterval(session.timer);
    const local = session.local?.getTracks() || [];
    const remote = session.remote.getTracks();
    for (const track of [...local, ...remote]) track.stop();
    session.oscillator?.stop();
    session.sender.close();
    session.receiver.close();
    voiceAudio.pause();
    voiceAudio.srcObject = null;
    await session.context.close();
    voiceState.cleanup = { localTracksEnded: local.every((track) => track.readyState === "ended"), remoteTracksEnded: remote.every((track) => track.readyState === "ended"),
      peersClosed: session.sender.connectionState === "closed" && session.receiver.connectionState === "closed", audioReleased: voiceAudio.srcObject === null,
      audioContextClosed: session.context.state === "closed", timerCleared: true };
    voiceState.sender = session.sender.connectionState;
    voiceState.receiver = session.receiver.connectionState;
  }
  voiceState.status = "stopped";
  renderVoice();
}

async function showVoiceError(error) {
  if (error.name === "NotAllowedError") voiceState.permission = "denied";
  await stopVoice();
  voiceState.status = "error";
  voiceError.textContent = error.message || "媒体连接没有成功";
  voiceError.hidden = false;
  renderVoice();
}

async function startVoice() {
  if (activeVoice) return;
  voiceError.hidden = true;
  voiceState = { status: "starting", source: voiceSource.value, permission: "unknown", sender: "new", receiver: "new", localAudioTracks: 0, remoteAudioTracks: 0,
    packetsReceived: 0, bytesReceived: 0, totalAudioEnergy: 0, peakRms: 0, cleanup: null };
  const session = { sender: new RTCPeerConnection({ iceServers: [] }), receiver: new RTCPeerConnection({ iceServers: [] }),
    remote: new MediaStream(), context: new AudioContext(), local: null, timer: null, analyser: null, oscillator: null };
  activeVoice = session;
  renderVoice();
  try {
    await session.context.resume();
    const stream = await localAudio(session);
    if (activeVoice !== session) {
      for (const track of stream.getTracks()) track.stop();
      return;
    }
    session.local = stream;
    voiceState.localAudioTracks = stream.getAudioTracks().length;
    await negotiate(session);
    if (activeVoice === session) session.timer = setInterval(() => sampleVoice(session).catch((error) => sessionError(session, error)), 200);
  } catch (error) {
    if (activeVoice === session) await showVoiceError(error);
  }
}

voiceStart.addEventListener("click", startVoice);
voiceStop.addEventListener("click", stopVoice);
voiceSource.addEventListener("change", renderVoice);
window.addEventListener("pagehide", stopVoice);
window.voiceHarness = Object.freeze({ snapshot: () => structuredClone(voiceState) });
refreshDevices().catch(() => { voiceDevice.disabled = true; });
renderVoice();
