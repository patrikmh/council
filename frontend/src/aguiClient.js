// AG-UI client. EventSource can't POST, so we read the SSE stream off a
// fetch body and parse frames by hand. Each `data:` line is one AG-UI event.

async function streamAGUI(url, body, onEvent, signal) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify(body),
  });
  if (!res.body) {
    throw new Error(`Backend responded ${res.status}`);
  }
  if (!res.ok && res.status !== 429 && res.status !== 400 && res.status !== 409) {
    throw new Error(`Backend responded ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          onEvent(JSON.parse(line.slice(5)));
        } catch {
          // ignore malformed frames
        }
      }
    }
  }
}

export function runAgent({ question, threadId, selectedModels, signal, onEvent }) {
  return streamAGUI(
    "/agui",
    {
      threadId,
      runId: crypto.randomUUID(),
      messages: [{ role: "user", content: question }],
      selectedModels: selectedModels ?? [],
    },
    onEvent,
    signal,
  );
}

export function runDebate({ question, threadId, selectedModels, signal, onEvent }) {
  return streamAGUI(
    "/agui/debate",
    {
      threadId,
      runId: crypto.randomUUID(),
      messages: [{ role: "user", content: question }],
      selectedModels: selectedModels ?? [],
    },
    onEvent,
    signal,
  );
}

// No question — the edition slot is decided server-side and the council
// is the fixed news panel. watchOnly attaches to a live run if one
// exists but never triggers a new (costly) generation.
export function runNews({ threadId, watchOnly, signal, onEvent }) {
  return streamAGUI(
    "/agui/news",
    {
      threadId,
      runId: crypto.randomUUID(),
      messages: [],
      selectedModels: [],
      watchOnly: watchOnly ?? false,
    },
    onEvent,
    signal,
  );
}
