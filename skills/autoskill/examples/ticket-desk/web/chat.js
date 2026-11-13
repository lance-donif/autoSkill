"use strict";

const chatForm = document.getElementById("chat-form");
const chatQuestion = document.getElementById("chat-question");
const chatMessages = document.getElementById("chat-messages");
const chatError = document.getElementById("chat-error");
const chatSend = document.getElementById("chat-send");
const chatStatus = document.getElementById("chat-status");
let conversationId = null;
let turn = 0;

function message(label, text) {
  const entry = document.createElement("p");
  const heading = document.createElement("strong");
  const content = document.createElement("span");
  heading.textContent = label + "：";
  content.textContent = text;
  entry.append(heading, content);
  chatMessages.append(entry);
  return content;
}

function acceptEvent(block, answer) {
  const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
  if (!data) return false;
  const value = JSON.parse(data);
  if (value.conversation_id) conversationId = value.conversation_id;
  if (value.turn) turn = value.turn;
  if (typeof value.delta === "string") answer.textContent += value.delta;
  return value.done === true;
}

async function receiveStream(response, answer) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let complete = false;
  try {
    while (!complete) {
      const chunk = await reader.read();
      pending += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      let boundary = pending.indexOf("\n\n");
      while (boundary !== -1) {
        complete = acceptEvent(pending.slice(0, boundary), answer) || complete;
        pending = pending.slice(boundary + 2);
        boundary = pending.indexOf("\n\n");
      }
      if (chunk.done) break;
    }
    if (!complete) throw new Error("流式响应未正常完成");
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = chatQuestion.value.trim();
  if (!question || chatSend.disabled) return;
  const streaming = document.getElementById("chat-protocol").value === "sse";
  message("问题", question);
  const answer = message("回答", "");
  chatSend.disabled = true;
  chatError.hidden = true;
  try {
    const response = await fetch(streaming ? "/api/chat/stream" : "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: conversationId }),
    });
    if (!response.ok) throw new Error("请求失败：" + response.status);
    if (streaming) {
      await receiveStream(response, answer);
    } else {
      const result = await response.json();
      conversationId = result.conversation_id;
      turn = result.turn;
      answer.textContent = JSON.stringify(result.answer);
    }
    chatStatus.textContent = "已完成第 " + turn + " 轮对话";
    chatQuestion.value = "";
    chatQuestion.focus();
  } catch (error) {
    chatError.textContent = error.message;
    chatError.hidden = false;
  } finally {
    chatSend.disabled = false;
  }
});

document.getElementById("chat-reset").addEventListener("click", () => {
  conversationId = null;
  turn = 0;
  chatMessages.replaceChildren();
  chatError.hidden = true;
  chatStatus.textContent = "已新建对话";
  chatQuestion.focus();
});
