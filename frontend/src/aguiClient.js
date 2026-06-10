// AG-UI client. EventSource can't POST, so we read the SSE stream off a
// fetch body and parse frames by hand. Each `data:` line is one AG-UI event.

export async function runAgent({ question, threadId, selectedModels, signal, onEvent }) {
  const res = await fetch("/agui", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      threadId,
      runId: crypto.randomUUID(),
      messages: [{ role: "user", content: question }],
      selectedModels: selectedModels ?? [],
    }),
  });
  if (!res.body) {
    throw new Error(`Backend responded ${res.status}`);
  }

  // For 429/400, the body is still an SSE stream with a run_error event —
  // read it so the UI shows the guard message instead of a generic error.
  // For other non-OK codes, throw immediately.
  if (!res.ok && res.status !== 429 && res.status !== 400) {
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
