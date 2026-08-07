import { useMemo, useState } from "react";
import { HttpAgent } from "@ag-ui/client";
import {
  AssistantRuntimeProvider,
  ThreadPrimitive,
  MessagePrimitive,
  ComposerPrimitive,
} from "@assistant-ui/react";
import {
  useAgUiRuntime,
  useAgUiInterrupts,
  useAgUiSubmitInterruptResponses,
} from "@assistant-ui/react-ag-ui";
import "./App.css";

const AGUI_URL = import.meta.env.VITE_ORCHESTRATOR_AGUI_URL ?? "http://localhost:8000/agui";

function InterruptPanel() {
  // Native AG-UI hooks - no custom event parsing needed. Backed by
  // orchestrator_agui_server.py's RunFinishedInterruptOutcome, which reports
  // EVERY pending interrupt in a wave at once (not just the first) - see
  // docs/BUG_AG_UI_MULTI_INTERRUPT.md for why that took real work to get right.
  const interrupts = useAgUiInterrupts();
  const submitInterruptResponses = useAgUiSubmitInterruptResponses();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  if (interrupts.length === 0) return null;

  const submitOne = async (interruptId: string) => {
    const answer = answers[interruptId]?.trim();
    if (!answer) return;
    setSubmitting(true);
    try {
      await submitInterruptResponses([{ interruptId, status: "resolved", payload: answer }]);
      setAnswers((prev) => {
        const next = { ...prev };
        delete next[interruptId];
        return next;
      });
    } finally {
      setSubmitting(false);
    }
  };

  const submitAll = async () => {
    const entries = interrupts
      .filter((i) => answers[i.id]?.trim())
      .map((i) => ({ interruptId: i.id, status: "resolved" as const, payload: answers[i.id].trim() }));
    if (entries.length === 0) return;
    setSubmitting(true);
    try {
      await submitInterruptResponses(entries);
      setAnswers({});
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="interrupt-panel">
      <div className="interrupt-panel-header">
        {interrupts.length === 1
          ? "The orchestrator needs more information:"
          : `The orchestrator needs ${interrupts.length} things clarified (answer any number, others stay pending):`}
      </div>
      {interrupts.map((interrupt) => (
        <div key={interrupt.id} className="interrupt-card">
          <div className="interrupt-question">{interrupt.message ?? "More information needed."}</div>
          <div className="interrupt-input-row">
            <input
              type="text"
              value={answers[interrupt.id] ?? ""}
              onChange={(e) => setAnswers((prev) => ({ ...prev, [interrupt.id]: e.target.value }))}
              onKeyDown={(e) => e.key === "Enter" && submitOne(interrupt.id)}
              placeholder="Your answer..."
              disabled={submitting}
            />
            <button onClick={() => submitOne(interrupt.id)} disabled={submitting || !answers[interrupt.id]?.trim()}>
              Answer
            </button>
          </div>
        </div>
      ))}
      {interrupts.length > 1 && (
        <button className="interrupt-submit-all" onClick={submitAll} disabled={submitting}>
          Submit all answered
        </button>
      )}
    </div>
  );
}

function ChatMessage() {
  return (
    <MessagePrimitive.Root className="message">
      <div className="message-content">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function Composer() {
  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Ask the orchestrator to do something..."
        autoFocus
      />
      <ComposerPrimitive.Send className="composer-send">Send</ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}

function Chat() {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="thread-empty">
            Try: "What's the weather in Tokyo?" or "Research rate limiting best practices and write example code."
          </div>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages components={{ Message: ChatMessage }} />
      </ThreadPrimitive.Viewport>
      <InterruptPanel />
      <Composer />
    </ThreadPrimitive.Root>
  );
}

export default function App() {
  const agent = useMemo(() => new HttpAgent({ url: AGUI_URL }), []);
  const runtime = useAgUiRuntime({ agent });

  return (
    <div className="app">
      <header className="app-header">
        <h1>Agent System</h1>
        <span className="app-subtitle">LangGraph orchestrator via AG-UI</span>
      </header>
      <AssistantRuntimeProvider runtime={runtime}>
        <Chat />
      </AssistantRuntimeProvider>
    </div>
  );
}
