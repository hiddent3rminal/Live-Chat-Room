(() => {
  "use strict";

  const root = document.body;
  const usernameEl = document.getElementById("username");
  if (!usernameEl) return;

  let username = root.dataset.username;
  let currentRoomId = null;
  let currentRoomName = "General";
  let currentRecipient = null;
  let currentRoomOwner = null;
  let selectedFile = null;
  let mediaRecorder = null;
  let typingTimer = null;
  let lastTyping = 0;
  let currentMessages = [];
  let oldestMessageId = null;
  let hasOlderMessages = false;
  const unread = new Map();
  let sendPending = false;
  let sendAttempt = 0;
  let pendingAttachmentId = null;
  let pendingAttachmentFile = null;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const realtimeAvailable = typeof io === "function";
  const socket = realtimeAvailable
    ? io()
    : { connected: false, on() {}, emit() {} };
  const chat = document.getElementById("chat");
  let inviteRoomId = null;

  document.querySelectorAll("[data-dialog-cancel]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-CSRF-Token", csrf);
    if (options.body && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(url, { ...options, headers });
    const result = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
    
    return result;
  }

  function showError(message) {
    let alert = document.getElementById("app-alert");
    if (!alert) {
      alert = document.createElement("div");
      alert.id = "app-alert";
      alert.setAttribute("role", "alert");
      document.querySelector(".main-chat").prepend(alert);
    }
    alert.textContent = message;
    alert.hidden = false;
    window.clearTimeout(alert.timer);
    alert.timer = window.setTimeout(() => { alert.hidden = true; }, 5000);
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function addRoomEntry(room) {
    const roomId = Number(room.id);
    if (document.querySelector(`.room-item[data-room-id="${roomId}"]`)) return;

    const entry = node("div", "room-entry");
    const button = node("button", "room-item");
    button.type = "button";
    button.dataset.roomId = roomId;
    button.dataset.roomName = room.name;
    button.dataset.ownerId = room.owner_id ?? "";
    button.append(node("span", "room-icon", "#"), node("span", "", room.name));
    if (room.is_private || room.private) {
      button.append(node("span", "", "🔒"));
    }
    entry.append(button);

    if ((room.is_private || room.private) &&
        String(room.owner_id) === root.dataset.userId) {
      const invite = node("button", "small-button", "Invite");
      invite.type = "button";
      invite.dataset.inviteRoom = roomId;
      invite.setAttribute("aria-label", `Invite user to ${room.name}`);
      entry.append(invite);
    }
    document.getElementById("rooms").append(entry);
  }

  async function syncRooms() {
    const result = await api("/api/rooms");
    result.rooms.forEach(addRoomEntry);
  }

  function attachmentNode(message) {
    if (!message.attachment_id) return null;
    const url = `/uploads/${encodeURIComponent(message.attachment_id)}`;
    const extension = (message.attachment_name.split(".").pop() || "").toLowerCase();
    let media;
    if (["png", "jpg", "jpeg", "gif", "webp"].includes(extension)) {
      media = node("img", "message-attachment");
      media.src = url;
      media.alt = message.attachment_name;
      media.loading = "lazy";
    } else if (["mp3", "ogg", "wav", "m4a"].includes(extension)) {
      media = node("audio", "message-attachment");
      media.controls = true;
      media.src = url;
    } else if (["webm", "mp4"].includes(extension)) {
      media = node("video", "message-attachment");
      media.controls = true;
      media.src = url;
    } else {
      media = node("a", "attachment-link", `📎 ${message.attachment_name}`);
      media.href = url;
      media.target = "_blank";
      media.rel = "noopener noreferrer";
    }
    return media;
  }

  function renderMessage(message) {
    const item = node("article", "message");
    item.dataset.messageId = message.id;
    if (message.sender_id === Number(root.dataset.userId)) item.classList.add("own");
    if (message.deleted) {
      item.append(node("em", "", "This message was removed."));
      return item;
    }

    const header = node("div", "message-header");
    const initial=message.sender.slice(0,1).toUpperCase();
    const avatar=node("span", "user-avatar", initial)
    avatar.dataset.userId=message.sender_id
    if (message.avatar_url){
      const img=document.createElement('img')
      img.className="user-avatar"
      img.src=message.avatar_url;
      img.alt=`${message.sender}'s profile picture`;
      img.dataset.userId=message.sender_id;
      img.addEventListener("error",()=>{
        img.replaceWith(avatar);
      },{once:true})
      header.append(img)
    }else{
      header.append(avatar)
    }
    header.append(node("strong", "", message.sender));
    if (message.pinned) header.append(node("span", "pinned-badge", "Pinned"));
    const date = new Date(message.created_at);
    header.append(node("time", "timestamp", Number.isNaN(date.valueOf()) ? "" :
      date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    if (message.sender_id === Number(root.dataset.userId) && message.read_count > 0) {
      header.append(node("span", "read-receipt", "Seen"));
    }
    item.append(header);

    if (message.body) {
      const body = node("p", "message-body", message.body);
      item.append(body);
    }
    const attachment = attachmentNode(message);
    if (attachment) item.append(attachment);

    const actions = node("div", "message-actions");
    if (message.room_id) {
      for (const emoji of ["👍", "❤️", "😂", "🎉"]) {
        const reaction = message.reactions.find((entry) => entry.emoji === emoji);
        const button = node("button", "reaction-button", `${emoji}${reaction ? ` ${reaction.count}` : ""}`);
        button.type = "button";
        button.setAttribute("aria-pressed", String(Boolean(reaction?.mine)));
        button.addEventListener("click", () => api(`/api/messages/${message.id}/react`, {
          method: "POST", body: JSON.stringify({ emoji }),
        }).then(() => refreshCurrentRoom()).catch((error) => showError(error.message)));
        actions.append(button);
      }
      const pin = node("button", "message-action", message.pinned ? "Unpin" : "Pin");
      pin.type = "button";
      pin.addEventListener("click", () => api(`/api/messages/${message.id}/pin`, {
        method: "POST", body: "{}",
      }).then(() => refreshCurrentRoom()).catch((error) => showError(error.message)));
      actions.append(pin);
      if (message.sender_id === Number(root.dataset.userId)) {
        const remove = node("button", "message-action", "Delete");
        remove.type = "button";
        remove.addEventListener("click", () => api(`/api/messages/${message.id}/delete`, {
          method: "POST", body: "{}",
        }).then(() => refreshCurrentRoom()).catch((error) => showError(error.message)));
        actions.append(remove);
      } else {
        const report = node("button", "message-action", "Report");
        report.type = "button";
        report.addEventListener("click", async () => {
          const reason = window.prompt("Why are you reporting this message?");
          if (!reason) return;
          try {
            await api(`/api/messages/${message.id}/report`, {
              method: "POST", body: JSON.stringify({ reason }),
            });
            showError("Report submitted to moderators.");
          } catch (error) {
            showError(error.message);
          }
        });
        actions.append(report);
      }
    }
    if (!message.room_id && message.sender_id !== Number(root.dataset.userId)) {
      const report = node("button", "message-action", "Report");
      report.type = "button";
      report.addEventListener("click", async () => {
        const reason = window.prompt("Why are you reporting this message?");
        if (!reason) return;
        try {
          await api(`/api/messages/${message.id}/report`, {
            method: "POST", body: JSON.stringify({ reason }),
          });
          showError("Report submitted to moderators.");
        } catch (error) {
          showError(error.message);
        }
      });
      actions.append(report);
    }
    if (message.reactions.length || actions.childElementCount) item.append(actions);
    return item;
  }

  function showMessages(messages) {
    currentMessages = messages.slice();
    chat.replaceChildren(...messages.map(renderMessage));
    chat.scrollTop = chat.scrollHeight;
  }

  async function refreshCurrentRoom() {
    if (!currentRoomId) return;
    const roomId = currentRoomId;
    const result = await api(`/api/history?room=${roomId}`);
    if (currentRoomId !== roomId) return;
    showMessages(result.messages);
    oldestMessageId = result.before;
    hasOlderMessages = result.has_more;
    document.getElementById("load-older").hidden = !hasOlderMessages;
  }

  function setRoom(id, name) {
    currentRoomId = Number(id);
    currentRoomName = name;
    currentRecipient = null;
    currentMessages = [];
    oldestMessageId = null;
    hasOlderMessages = false;
    document.getElementById("load-older").hidden = true;
    document.getElementById("block-user").hidden = true;
    const roomButton = document.querySelector(`.room-item[data-room-id="${currentRoomId}"]`);
    currentRoomOwner = roomButton?.dataset.ownerId || null;
    document.getElementById("ban-user").hidden =
      root.dataset.userRole !== "admin" && currentRoomOwner !== root.dataset.userId;
    document.getElementById("room-title").textContent = `# ${name}`;
    document.querySelectorAll(".room-item").forEach((button) => {
      button.classList.toggle("active-room", Number(button.dataset.roomId) === currentRoomId);
    });
    document.getElementById("search-results").hidden = true;
    document.getElementById("chat").hidden = false;
    unread.delete(String(currentRoomId));
    socket.emit("join", { room_id: currentRoomId });
    socket.emit("read", { room_id: currentRoomId });
    if (!socket.connected) {
      refreshCurrentRoom().catch((error) => showError(error.message));
    }
    updateUnreadBadges();
  }

  function setPrivateRecipient(name) {
    if (currentRoomId) socket.emit("leave", { room_id: currentRoomId });
    currentRecipient = name;
    currentRoomId = null;
    currentMessages = [];
    oldestMessageId = null;
    hasOlderMessages = false;
    document.getElementById("load-older").hidden = true;
    document.getElementById("ban-user").hidden = true;
    document.getElementById("search-results").hidden = true;
    document.getElementById("chat").hidden = false;
    document.getElementById("typing-indicator").textContent = "";
    document.getElementById("room-title").textContent = `Private message · ${name}`;
    const blockButton = document.getElementById("block-user");
    blockButton.hidden = false;
    api(`/api/users/${encodeURIComponent(name)}/block`)
      .then((result) => {
        if (currentRecipient?.toLowerCase() === name.toLowerCase()) {
          blockButton.textContent = result.blocked ? "Unblock user" : "Block user";
        }
      })
      .catch((error) => showError(error.message));
    document.querySelectorAll(".room-item").forEach((button) => button.classList.remove("active-room"));
    api(`/api/private-history?user=${encodeURIComponent(name)}`)
      .then((result) => {
        if (currentRecipient?.toLowerCase() === name.toLowerCase()) {
          showMessages(result.messages);
        }
      })
      .catch((error) => showError(error.message));
  }

  function updateUnreadBadges() {
    document.querySelectorAll(".room-item").forEach((button) => {
      const count = unread.get(button.dataset.roomId) || 0;
      let badge = button.querySelector(".unread-badge");
      if (count) {
        if (!badge) {
          badge = node("span", "unread-badge");
          button.append(badge);
        }
        badge.textContent = String(count);
      } else if (badge) {
        badge.remove();
      }
    });
  }

  document.getElementById("rooms").addEventListener("click", (event) => {
    const button = event.target.closest(".room-item");
    if (button) setRoom(button.dataset.roomId, button.dataset.roomName);
  });

  document.getElementById("active-users").addEventListener("click", (event) => {
    const button = event.target.closest("[data-username]");
    if (button && button.dataset.username !== username) setPrivateRecipient(button.dataset.username);
  });

  socket.on("connect", () => {
    const active = document.querySelector(".room-item.active-room") ||
      document.querySelector(".room-item[data-room-name='General']") ||
      document.querySelector(".room-item");
    if (active) setRoom(active.dataset.roomId, active.dataset.roomName);
  });
  socket.on("connect_error", () => {
    syncRooms().catch((error) => showError(error.message));
  });
  socket.on("disconnect", () => {
    syncRooms().catch((error) => showError(error.message));
  });

  if (!realtimeAvailable) {
    showError("Live updates are unavailable. Room invitations will still be saved; refresh to see them.");
  }
  syncRooms().catch((error) => showError(error.message));
  window.addEventListener("focus", () => {
    syncRooms().catch((error) => showError(error.message));
  });
  window.setInterval(() => {
    if (!socket.connected) syncRooms().catch((error) => showError(error.message));
  }, 15000);

  socket.on("history", (data) => {
    if (Number(data.room_id) === currentRoomId) {
      showMessages(data.messages);
      oldestMessageId = data.messages[0]?.id || null;
      hasOlderMessages = data.messages.length === 100;
      document.getElementById("load-older").hidden = !hasOlderMessages;
    }
  });

  socket.on("room_invited", (data) => {
    if (!data.room) return;
    addRoomEntry(data.room);
    showError(`You were invited to # ${data.room.name}.`);
  });

  socket.on("message", (message) => {
    if (Number(message.room_id) === currentRoomId) {
      chat.append(renderMessage(message));
      currentMessages.push(message);
      chat.scrollTop = chat.scrollHeight;
      socket.emit("read", { room_id: currentRoomId });
    } else if (message.room_id) {
      const key = String(message.room_id);
      unread.set(key, (unread.get(key) || 0) + 1);
      updateUnreadBadges();
    }
    if (message.sender_id !== Number(root.dataset.userId) &&
        document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification(`${message.sender} · ${currentRoomName}`, {
        body: message.body || "Sent an attachment.",
      });
    }
  });

  socket.on("private_message", (message) => {
    if (currentRecipient &&
        (message.sender === currentRecipient ||
         (message.sender_id === Number(root.dataset.userId) &&
          message.recipient?.toLowerCase() === currentRecipient.toLowerCase()))) {
      chat.append(renderMessage(message));
      currentMessages.push(message);
      chat.scrollTop = chat.scrollHeight;
    }
  });

  socket.on("message_update", () => refreshCurrentRoom().catch((error) => showError(error.message)));
  socket.on("error_message", (data) => showError(data.error));
  socket.on("room_banned", (data) => {
    if (Number(data.room_id) !== currentRoomId) return;
    showError("You were banned from this room.");
    const general = document.querySelector(".room-item[data-room-name='General']");
    if (general) setRoom(general.dataset.roomId, general.dataset.roomName);
  });
  socket.on("presence", (data) => {
    const users = document.getElementById("active-users");
    users.replaceChildren(...data.users.map((user) => {
      const button = node("button", "user-item", "");
      button.dataset.username = user.username;
      const initial=user.username.slice (0,1).toUpperCase();
      const avatar = node("span", "user-avatar", initial);
      avatar.dataset.userId=user.user_id
      if (user.avatar_url){
        const img= document.createElement("img")
        img.className= "user-avatar"
        img.src=user.avatar_url;
        img.alt=`${user.username}'s profile picture`
        img.dataset.userId=user.user_id
        img.addEventListener("error",()=>{
          img.replaceWith(avatar);
        }, {once:true})
        button.append(img)
      }else{
        button.append(avatar)
      }
      button.append( document.createTextNode(`${user.username}${user.room ? ` · ${user.room}` : ""}`));
      if (user.username.toLowerCase() === username.toLowerCase()) button.classList.add("me");
      return button;
    }));
  });

  socket.on("avatar_updated", (data) => {
    document.querySelectorAll(`[data-user-id="${data.user_id}"]`).forEach((element)=> {
      if (element.tagName==="IMG"){
        element.src=data.avatar_url
      }else if(element.tagName ==="SPAN"){
        const img=document.createElement("img")
        img.className="user-avatar"
        img.src=data.avatar_url
        img.dataset.userId=data.user_id;
        img.alt="User profile picture";
        img.addEventListener("error",()=>{
            img.replaceWith(element);
        },{once:true})
        element.replaceWith(img)
      }
    })
  })

  socket.on("typing", (data) => {
    const indicator = document.getElementById("typing-indicator");
    indicator.textContent = data.typing ? `${data.username} is typing…` : "";
    window.clearTimeout(typingTimer);
    if (data.typing) typingTimer = window.setTimeout(() => { indicator.textContent = ""; }, 2500);
  });

  document.getElementById("message").addEventListener("input", () => {
    const now = Date.now();
    if (now - lastTyping < 800) return;
    lastTyping = now;
    socket.emit("typing", {
      room_id: currentRoomId,
      recipient: currentRecipient,
      typing: true,
    });
  });

  document.getElementById("message-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("message");
    const form = event.currentTarget;
    const sendButton = form.querySelector('button[type="submit"]');
    if (sendPending) return;
    let body = input.value.trim();
    let mentionedRecipient = null;
    if (!currentRecipient && body.startsWith("@")) {
      const match = body.match(/^@([A-Za-z0-9_]{3,24})\s+([\s\S]+)$/);
      if (match) {
        mentionedRecipient = match[1];
        body = match[2].trim();
      }
    }
    if (!body && !selectedFile) return;
    if (!socket.connected) {
      showError("The chat is not connected. Your message was not sent; please try again when it reconnects.");
      return;
    }
    if (mentionedRecipient) setPrivateRecipient(mentionedRecipient);
    sendPending = true;
    sendButton.disabled = true;
    const attempt = ++sendAttempt;
    const submittedValue = input.value;
    const submittedFile = selectedFile;
    try {
      let attachmentId;
      if (submittedFile) {
        if (pendingAttachmentFile === submittedFile) {
          attachmentId = pendingAttachmentId;
        } else {
          const uploadForm = new FormData();
          uploadForm.append("file", submittedFile);
          const upload = await api("/api/uploads", { method: "POST", body: uploadForm });
          attachmentId = upload.attachment_id;
          pendingAttachmentId = attachmentId;
          pendingAttachmentFile = submittedFile;
        }
      }
      const acknowledgmentTimer = window.setTimeout(() => {
        if (attempt !== sendAttempt) return;
        sendPending = false;
        sendButton.disabled = false;
        showError("The server has not confirmed your message. It has been kept so you can retry.");
      }, 15000);
      socket.emit("message", {
        body,
        attachment_id: attachmentId,
        room_id: currentRoomId,
        recipient: currentRecipient,
      }, (result) => {
        if (attempt !== sendAttempt) return;
        window.clearTimeout(acknowledgmentTimer);
        sendPending = false;
        sendButton.disabled = false;
        if (!result?.ok) return;
        if (input.value === submittedValue) input.value = "";
        if (selectedFile === submittedFile) {
          selectedFile = null;
          document.getElementById("file-input").value = "";
          document.getElementById("attachment-name").textContent = "";
        }
        if (pendingAttachmentId === attachmentId) {
          pendingAttachmentId = null;
          pendingAttachmentFile = null;
        }
        input.focus();
      });
    } catch (error) {
      sendAttempt += 1;
      sendPending = false;
      sendButton.disabled = false;
      showError(error.message);
    }
  });

  document.getElementById("attach-button").addEventListener("click", () =>
    document.getElementById("file-input").click());
  document.getElementById("file-input").addEventListener("change", (event) => {
    selectedFile = event.target.files[0] || null;
    pendingAttachmentId = null;
    pendingAttachmentFile = null;
    document.getElementById("attachment-name").textContent = selectedFile ? selectedFile.name : "";
  });

  document.getElementById("record-button").addEventListener("click", async () => {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      showError("Voice recording is not supported by this browser.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) chunks.push(event.data);
      });
      mediaRecorder.addEventListener("stop", () => {
        const type = mediaRecorder.mimeType || "audio/webm";
        selectedFile = new File(chunks, `voice-note.${type.includes("ogg") ? "ogg" : "webm"}`, { type });
        pendingAttachmentId = null;
        pendingAttachmentFile = null;
        document.getElementById("file-input").value = "";
        document.getElementById("attachment-name").textContent = "Voice note ready to send";
        stream.getTracks().forEach((track) => track.stop());
        document.getElementById("record-button").hidden = false;
        document.getElementById("stop-recording").hidden = true;
      }, { once: true });
      mediaRecorder.start();
      document.getElementById("record-button").hidden = true;
      document.getElementById("stop-recording").hidden = false;
    } catch {
      showError("Microphone access was denied or is unavailable.");
    }
  });
  document.getElementById("stop-recording").addEventListener("click", () => {
    if (mediaRecorder?.state === "recording") mediaRecorder.stop();
  });

  document.getElementById("create-room").addEventListener("click", () =>
    document.getElementById("create-room-dialog").showModal());
  document.getElementById("create-room-submit").addEventListener("click", async () => {
    const error = document.getElementById("room-error");
    try {
      const result = await api("/api/rooms", {
        method: "POST",
        body: JSON.stringify({
          name: document.getElementById("new-room-name").value.trim(),
          description: document.getElementById("new-room-description").value.trim(),
          private: document.getElementById("new-room-private").checked,
        }),
      });
      document.getElementById("create-room-dialog").close();
      addRoomEntry({ ...result, owner_id: root.dataset.userId });
      setRoom(result.id, result.name);
    } catch (exception) {
      error.textContent = exception.message;
    }
  });

  document.getElementById("change-name").addEventListener("click", () => {
    document.getElementById("profile-name").value = username;
    document.getElementById("profile-dialog").showModal();
  });

let selectedAvatarFile = null;
let avatarPreviewUrl = null;

const avatarinput = document.getElementById("profile-avatar-input");
const avatarpreview = document.getElementById("profile-avatar-preview");

avatarinput.addEventListener("change", (event) => {
    const file = event.target.files[0];

    if (!file) return;

    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
        showError("Please select an image.");
        avatarinput.value = "";
        selectedAvatarFile = null;
        return;
    }

    if (file.size > 4 * 1024 * 1024) {
        showError("The image must be 4MB or less.");
        avatarinput.value = "";
        selectedAvatarFile = null;
        return;
    }

    selectedAvatarFile = file;

    if (avatarPreviewUrl) {
        URL.revokeObjectURL(avatarPreviewUrl);
    }

    avatarPreviewUrl = URL.createObjectURL(file);
    avatarpreview.src = avatarPreviewUrl;
});


function UpdateMyavatar(avatarurl){
  const avatars=document.querySelectorAll(
    '[data-user-id="'+ currentUserId + '"] .user-avatar'
  )
  avatars.forEach((avatar)=> {
    avatar.src=avatarurl
  })

  const headerAvatar=document.getElementById("header-avatar")
  const profileAvatar=document.getElementById("profile-avatar-preview");
  if (headerAvatar) headerAvatar.src=avatarurl;
  if (profileAvatar) profileAvatar.src=avatarurl;
}






  document.getElementById("block-user").addEventListener("click", async (event) => {
    if (!currentRecipient) return;
    const blocking = event.currentTarget.textContent === "Block user";
    try {
      const result = await api(`/api/users/${encodeURIComponent(currentRecipient)}/block`, {
        method: blocking ? "POST" : "DELETE",
      });
      event.currentTarget.textContent = result.blocked ? "Unblock user" : "Block user";
      if (result.blocked) showError(`${currentRecipient} is blocked.`);
    } catch (error) {
      showError(error.message);
    }
  });

  document.getElementById("ban-user").addEventListener("click", async () => {
    if (!currentRoomId) return;
    const target = window.prompt("Username to ban from this room:");
    if (!target) return;
    try {
      await api(`/api/rooms/${currentRoomId}/ban`, {
        method: "POST",
        body: JSON.stringify({ username: target }),
      });
      showError(`${target} was banned from ${currentRoomName}.`);
    } catch (error) {
      showError(error.message);
    }
  });

  document.getElementById("rooms").addEventListener("click", async (event) => {
    const inviteButton = event.target.closest("[data-invite-room]");
    if (!inviteButton) return;
    event.stopPropagation();
    inviteRoomId = inviteButton.dataset.inviteRoom;
    document.getElementById("invite-username").value = "";
    document.getElementById("invite-error").textContent = "";
    document.getElementById("invite-dialog").showModal();
    document.getElementById("invite-username").focus();
  });

  document.getElementById("invite-submit").addEventListener("click", async () => {
    const invitee = document.getElementById("invite-username").value.trim();
    const error = document.getElementById("invite-error");
    if (!inviteRoomId || !invitee) {
      error.textContent = "Enter the username of the person to invite.";
      return;
    }
    try {
      await api(`/api/rooms/${inviteRoomId}/invite`, {
        method: "POST",
        body: JSON.stringify({ username: invitee }),
      });
      document.getElementById("invite-dialog").close();
      showError(`${invitee} can now join this private room.`);
    } catch (error) {
      document.getElementById("invite-error").textContent = error.message;
    }
  });
document.getElementById("profile-save").addEventListener("click", async () => {
    const profileError = document.getElementById("profile-error");

    profileError.textContent = "";

    try {
        if (selectedAvatarFile) {
            const formData = new FormData();
            formData.append("avatar", selectedAvatarFile);

            const avatarResult = await api("/api/avatar", {
                method: "POST",
                body: formData
            });

            avatarpreview.src = avatarResult.avatar_url;
            

            document.querySelectorAll(".user-avatar").forEach((avatar)=>{
              avatar.src=avatarResult.avatar_url;
            })

            selectedAvatarFile = null;
            avatarinput.value = "";

            if (avatarPreviewUrl) {
                URL.revokeObjectURL(avatarPreviewUrl);
                avatarPreviewUrl = null;
            }
        }

        const result = await api("/api/profile", {
            method: "POST",
            body: JSON.stringify({
                username: document.getElementById("profile-name").value.trim()
            }),
        });

        username = result.username;
        usernameEl.textContent = username;
        root.dataset.username = username;

        document.getElementById("profile-dialog").close();

    } catch (error) {
        profileError.textContent = error.message;
    }
});
  document.getElementById("search-toggle").addEventListener("click", () => {
    document.getElementById("search-panel").hidden = false;
    document.getElementById("search-query").focus();
  });
  document.getElementById("search-close").addEventListener("click", () => {
    document.getElementById("search-panel").hidden = true;
    document.getElementById("search-results").hidden = true;
  });
  document.getElementById("search-submit").addEventListener("click", async () => {
    const query = document.getElementById("search-query").value.trim();
    try {
      const result = await api(`/api/search?q=${encodeURIComponent(query)}${currentRoomId ? `&room=${currentRoomId}` : ""}`);
      const results = document.getElementById("search-results");
      results.replaceChildren(...result.messages.map(renderMessage));
      results.hidden = false;
      chat.hidden = true;
    } catch (error) {
      showError(error.message);
    }
  });
  document.getElementById("load-older").addEventListener("click", async () => {
    if (!currentRoomId || !oldestMessageId || !hasOlderMessages) return;
    try {
      const previousHeight = chat.scrollHeight;
      const result = await api(
        `/api/history?room=${currentRoomId}&before=${oldestMessageId}`,
      );
      currentMessages = [...result.messages, ...currentMessages];
      chat.replaceChildren(...currentMessages.map(renderMessage));
      chat.scrollTop = chat.scrollHeight - previousHeight;
      oldestMessageId = result.before;
      hasOlderMessages = result.has_more;
      document.getElementById("load-older").hidden = !hasOlderMessages;
    } catch (error) {
      showError(error.message);
    }
  });
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark-theme");
    localStorage.setItem("chat-theme", dark ? "dark" : "light");
  });
  if (localStorage.getItem("chat-theme") === "dark") document.documentElement.classList.add("dark-theme");

  document.getElementById("notifications-toggle").addEventListener("click", async (event) => {
    if (!("Notification" in window)) {
      showError("Browser notifications are not supported.");
      return;
    }
    const button = event.currentTarget;
    const permission = await Notification.requestPermission();
    button.textContent = permission === "granted" ? "Notifications enabled" : "Notifications blocked";
  });

  document.querySelector(".fab-menu").addEventListener("click", () => {
    document.querySelector(".sidebar").classList.toggle("active");
  });
})();
