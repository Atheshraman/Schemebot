const conversations = [
  {
    id: "rituals",
    title: "Morning Rituals",
    last: "Can we keep it gentle and upbeat?",
    messages: [
      {
        role: "assistant",
        text: "Good morning. I can keep everything gentle and upbeat. Want a 3-step ritual?",
        time: "8:12 AM",
      },
      {
        role: "user",
        text: "Yes, and keep it under 10 minutes.",
        time: "8:13 AM",
      },
      {
        role: "assistant",
        text: "Try this: 1) open the window + 4 deep breaths, 2) 2-minute stretch, 3) write one intention.",
        time: "8:14 AM",
      },
    ],
  },
  {
    id: "finance",
    title: "Finance Check-in",
    last: "Track salary and tax bracket notes.",
    messages: [
      {
        role: "assistant",
        text: "Got it. I can keep a simple summary of salary notes and your tax bracket preferences.",
        time: "7:45 PM",
      },
      {
        role: "user",
        text: "Please remind me before the end of the month.",
        time: "7:46 PM",
      },
    ],
  },
  {
    id: "travel",
    title: "Weekend Escape",
    last: "Prefer cozy cabins and soft lighting.",
    messages: [
      {
        role: "assistant",
        text: "Cozy cabins noted. Do you want lake or forest views?",
        time: "2:30 PM",
      },
      {
        role: "user",
        text: "Forest views, please.",
        time: "2:31 PM",
      },
    ],
  },
];

const memoryItems = [
  { label: "Name", value: "Jamie Patel" },
  { label: "Age", value: "29" },
  { label: "Preference", value: "Gentle, upbeat tone" },
  { label: "Salary", value: "$92k (approx.)" },
  { label: "Tax bracket", value: "22% federal" },
  { label: "Other", value: "Prefers cozy spaces & soft lighting" },
];

const elements = {
  convoList: document.querySelector("[data-conversation-list]"),
  messageList: document.querySelector("[data-message-list]"),
  convoCount: document.querySelector("[data-convo-count]"),
  chatTitle: document.querySelector("[data-chat-title]"),
  themeToggle: document.querySelector("[data-theme-toggle]"),
  sidebar: document.querySelector("[data-sidebar]"),
  sidebarToggle: document.querySelector("[data-sidebar-toggle]"),
  memoryToggle: document.querySelector("[data-memory-toggle]"),
  memoryPanel: document.querySelector("[data-memory-panel]"),
  memoryGrid: document.querySelector("[data-memory-grid]"),
  memorySection: document.querySelector("[data-memory-section]"),
  voiceButton: document.querySelector("[data-voice-button]"),
};

const state = {
  activeConversationId: conversations[0].id,
};

const setTheme = (theme) => {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("chat-theme", theme);
  if (elements.themeToggle) {
    elements.themeToggle.checked = theme === "dark";
  }
};

const getPreferredTheme = () => {
  const stored = localStorage.getItem("chat-theme");
  if (stored) {
    return stored;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
};

const renderConversations = () => {
  elements.convoList.innerHTML = "";
  conversations.forEach((conversation) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-item";
    item.dataset.conversationId = conversation.id;
    if (conversation.id === state.activeConversationId) {
      item.classList.add("active");
    }

    const title = document.createElement("h3");
    title.textContent = conversation.title;

    const last = document.createElement("p");
    last.textContent = conversation.last;

    item.append(title, last);
    elements.convoList.appendChild(item);
  });

  elements.convoCount.textContent = `${conversations.length}`;
};

const renderMessages = () => {
  const conversation = conversations.find((item) => item.id === state.activeConversationId);
  if (!conversation) {
    return;
  }

  elements.chatTitle.textContent = conversation.title;
  elements.messageList.innerHTML = "";

  conversation.messages.forEach((message) => {
    const bubble = document.createElement("div");
    bubble.className = `message ${message.role}`;

    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${message.role} · ${message.time}`;

    const body = document.createElement("div");
    body.textContent = message.text;

    bubble.append(meta, body);
    elements.messageList.appendChild(bubble);
  });
};

const renderMemory = () => {
  elements.memoryGrid.innerHTML = "";
  memoryItems.forEach((item) => {
    const row = document.createElement("div");
    row.className = "memory-row";

    const label = document.createElement("span");
    label.textContent = item.label;

    const value = document.createElement("strong");
    value.textContent = item.value;

    row.append(label, value);
    elements.memoryGrid.appendChild(row);
  });
};

const setActiveConversation = (id) => {
  state.activeConversationId = id;
  renderConversations();
  renderMessages();
};

const toggleMemoryPanel = (event) => {
  if (event) {
    event.stopPropagation();
  }
  if (!elements.memoryPanel || !elements.memoryToggle) {
    return;
  }
  elements.memoryPanel.classList.toggle("open");
  elements.memoryToggle.classList.toggle("open");
};

const closeMemoryPanel = () => {
  if (!elements.memoryPanel || !elements.memoryToggle) {
    return;
  }
  elements.memoryPanel.classList.remove("open");
  elements.memoryToggle.classList.remove("open");
};

const toggleSidebar = () => {
  elements.sidebar.classList.toggle("open");
};

const toggleVoiceButton = () => {
  const isActive = elements.voiceButton.classList.toggle("active");
  elements.voiceButton.setAttribute("aria-pressed", String(isActive));
};

const wireEvents = () => {
  elements.themeToggle.addEventListener("change", (event) => {
    setTheme(event.target.checked ? "dark" : "light");
  });

  elements.convoList.addEventListener("click", (event) => {
    const item = event.target.closest(".conversation-item");
    if (!item) {
      return;
    }
    setActiveConversation(item.dataset.conversationId);
    if (window.innerWidth <= 860) {
      elements.sidebar.classList.remove("open");
    }
  });

  if (elements.memoryToggle) {
    elements.memoryToggle.addEventListener("click", toggleMemoryPanel);
  }
  if (elements.memoryPanel) {
    elements.memoryPanel.addEventListener("click", (event) => event.stopPropagation());
  }
  if (elements.sidebarToggle) {
    elements.sidebarToggle.addEventListener("click", toggleSidebar);
  }
  if (elements.voiceButton) {
    elements.voiceButton.addEventListener("click", toggleVoiceButton);
  }
  if (elements.memorySection) {
    document.addEventListener("click", (event) => {
      if (!elements.memorySection.contains(event.target)) {
        closeMemoryPanel();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeMemoryPanel();
    }
  });
};

const init = () => {
  setTheme(getPreferredTheme());
  renderConversations();
  renderMessages();
  renderMemory();
  wireEvents();
};

init();
