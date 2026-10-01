/* Event reducer shared by live transport and trace recovery. */
export function mergeEvents(existing, batch) {
  const bySeq = new Map(existing.filter(e => Number.isInteger(e.seq)).map(e => [e.seq, e]));
  for (const event of batch) if (Number.isInteger(event.seq)
    && !["stream.end", "access.revoked"].includes(event.kind)) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

export function answerMessages(events) {
  const calls = new Map();
  for (const e of mergeEvents([], events)) {
    if (!e.kind?.startsWith("answer.")) continue;
    const key = e.payload?.call_id || "answer";
    if (e.kind === "answer.start") calls.set(key, {text: "", ended: false});
    const message = calls.get(key) || {text: "", ended: false};
    if (e.kind === "answer.delta") message.text += e.payload?.delta || "";
    if (e.kind === "answer.end") message.ended = true;
    calls.set(key, message);
  }
  return [...calls.values()].filter(m => m.text);
}
