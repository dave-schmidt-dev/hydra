(() => {
  const evtSource = new EventSource("/events");
  const bannersEl = document.getElementById("banners");
  const listEl = document.getElementById("question-list");
  // Expose SSE state for e2e tests; harmless otherwise.
  window.hydraSSE = { connected: false };

  evtSource.onmessage = (msg) => {
    let data;
    try {
      data = JSON.parse(msg.data);
    } catch (e) {
      return;
    }
    if (!data || !data.type) return;

    if (data.type === "connected") {
      window.hydraSSE.connected = true;
      return;
    }

    if (data.type.startsWith("banner") || data.type === "session_finalizing") {
      addBanner(data);
      return;
    }
    if (data.q_id && listEl) {
      refreshQuestion(data);
    }
  };

  evtSource.onerror = () => {
    addBanner({ severity: "warning", message: "SSE connection lost; reconnecting" });
  };

  function addBanner(data) {
    if (!bannersEl) return;
    const node = document.createElement("div");
    node.className = "banner " + (data.severity || "info");
    node.textContent = data.message || data.type;
    bannersEl.prepend(node);
  }

  function refreshQuestion(data) {
    const id = "q-" + data.q_id;
    let node = document.getElementById(id);
    if (!node) {
      node = document.createElement("li");
      node.id = id;
      node.className = "question";
      listEl.prepend(node);
    }
    const existingTopic = node.querySelector(".q-topic");
    const topicText = data.topic || (existingTopic ? existingTopic.textContent : data.q_id);

    while (node.firstChild) node.removeChild(node.firstChild);

    const topicDiv = document.createElement("div");
    topicDiv.className = "q-topic";
    topicDiv.textContent = topicText;
    node.appendChild(topicDiv);

    const metaDiv = document.createElement("div");
    metaDiv.className = "q-meta muted";
    metaDiv.textContent = data.q_id + " - " + data.type;
    node.appendChild(metaDiv);
  }
})();
