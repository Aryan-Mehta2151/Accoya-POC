import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  BookOpenText,
  ExternalLink,
  MessageCircleMore,
  RefreshCw,
  RotateCcw,
  Sparkles,
} from "lucide-react";

import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { ChatMessage } from "../../types";
import { ErrorState, LoadingState, PageHeader } from "../../components/ui";
import styles from "./AssistantPage.module.css";

const SESSION_STORAGE_KEY = "accoya-outreach-assistant-session";

const SUGGESTED_PROMPTS = [
  "What should we emphasize when introducing Accoya to an architect?",
  "Summarize the strongest sustainability talking points.",
  "Which product benefits matter most for exterior applications?",
  "Help me prepare for a conversation about long-term performance.",
];

type ConversationMessage = ChatMessage & { id: string };

function storedSessionId() {
  try {
    return window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveSessionId(sessionId: string | null) {
  try {
    if (sessionId) window.sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    else window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // A private browser context may disallow storage. The active chat still works.
  }
}

function messageId(message: ChatMessage, index: number) {
  return `${message.role}-${message.created_at ?? "history"}-${index}`;
}

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return fallback;
}

function isWebUrl(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function AssistantPage() {
  const queryClient = useQueryClient();
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [sessionId, setSessionId] = useState<string | null>(storedSessionId);
  const [localMessages, setLocalMessages] = useState<ConversationMessage[]>([]);
  const [input, setInput] = useState("");
  const [lastSubmitted, setLastSubmitted] = useState<string | null>(null);

  const historyQuery = useQuery({
    queryKey: queryKeys.chatHistory(sessionId ?? ""),
    queryFn: () => api.getChatHistory(sessionId as string),
    enabled: Boolean(sessionId),
    refetchOnWindowFocus: false,
  });
  const historyMessages = useMemo(
    () =>
      (historyQuery.data ?? []).map((message, index) => ({
        ...message,
        id: messageId(message, index),
      })),
    [historyQuery.data],
  );
  const messages = localMessages.length > 0 ? localMessages : historyMessages;
  const isRestoring =
    Boolean(sessionId) && localMessages.length === 0 && historyQuery.isLoading;

  const chatMutation = useMutation({
    mutationFn: ({
      message,
      activeSessionId,
    }: {
      message: string;
      activeSessionId: string | null;
    }) => api.chat(message, activeSessionId),
    onSuccess: (response) => {
      setSessionId(response.session_id);
      saveSessionId(response.session_id);
      setLocalMessages((current) => [
        ...current,
        {
          id: `assistant-${Date.now()}`,
          role: "assistant",
          content: response.answer,
          sources: response.sources,
        },
      ]);
      setLastSubmitted(null);
    },
  });

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    transcript.scrollTo({
      top: transcript.scrollHeight,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }, [messages, chatMutation.isPending]);

  const submitMessage = (text: string, appendUser = true) => {
    const trimmed = text.trim();
    if (!trimmed || chatMutation.isPending || isRestoring) return;

    chatMutation.reset();
    setLastSubmitted(trimmed);
    if (appendUser) {
      setLocalMessages((current) => [
        ...(current.length > 0 ? current : historyMessages),
        {
          id: `user-${Date.now()}`,
          role: "user",
          content: trimmed,
        },
      ]);
      setInput("");
    }
    chatMutation.mutate({ message: trimmed, activeSessionId: sessionId });
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submitMessage(input);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      submitMessage(input);
    }
  };

  const startNewConversation = () => {
    const previousSessionId = sessionId;
    setSessionId(null);
    setLocalMessages([]);
    setInput("");
    setLastSubmitted(null);
    saveSessionId(null);
    chatMutation.reset();
    if (previousSessionId) {
      queryClient.removeQueries({
        queryKey: queryKeys.chatHistory(previousSessionId),
      });
    }
  };

  const hasMessages = messages.length > 0;

  return (
    <div className={styles.page}>
      <PageHeader
        eyebrow="Knowledge assistant"
        title="Ask with confidence"
        description="Explore the strategy and product knowledge available to your team."
        actions={
          <button
            className={styles.newConversationButton}
            type="button"
            disabled={chatMutation.isPending || (!sessionId && !hasMessages)}
            onClick={startNewConversation}
          >
            <RotateCcw size={16} aria-hidden="true" />
            New conversation
          </button>
        }
      />

      <section className={styles.assistant} aria-label="Knowledge assistant conversation">
        <div className={styles.assistantTopline}>
          <div className={styles.assistantIdentity}>
            <span className={styles.assistantMark} aria-hidden="true">
              <Sparkles size={18} />
            </span>
            <div>
              <strong>Accoya knowledge</strong>
              <span>Grounded in available indexed sources</span>
            </div>
          </div>
          <span className={styles.status}>
            <span aria-hidden="true" />
            Ready
          </span>
        </div>

        <div className={styles.transcript} ref={transcriptRef} aria-live="polite">
          {isRestoring && (
            <div className={styles.stateWrap}>
              <LoadingState label="Restoring your conversation…" />
            </div>
          )}

          {sessionId && historyQuery.isError && !hasMessages && (
            <div className={styles.stateWrap}>
              <ErrorState
                title="We couldn’t restore this conversation"
                message={errorMessage(
                  historyQuery.error,
                  "Start a new conversation or try loading this one again.",
                )}
                onRetry={() => void historyQuery.refetch()}
              />
              <button
                className={styles.resetLink}
                type="button"
                onClick={startNewConversation}
              >
                Start a new conversation
              </button>
            </div>
          )}

          {!isRestoring && !historyQuery.isError && !hasMessages && (
            <div className={styles.welcome}>
              <span className={styles.welcomeIcon} aria-hidden="true">
                <BookOpenText size={27} />
              </span>
              <p className={styles.welcomeKicker}>A thoughtful starting point</p>
              <h2>What would you like to understand?</h2>
              <p className={styles.welcomeCopy}>
                Ask a specific question, or begin with one of these prompts.
              </p>
              <div className={styles.suggestions} aria-label="Suggested questions">
                {SUGGESTED_PROMPTS.map((prompt) => (
                  <button
                    type="button"
                    key={prompt}
                    onClick={() => submitMessage(prompt)}
                  >
                    <MessageCircleMore size={17} aria-hidden="true" />
                    <span>{prompt}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasMessages && (
            <div className={styles.messages}>
              {messages.map((message) => (
                <article
                  className={`${styles.message} ${
                    message.role === "user"
                      ? styles.userMessage
                      : styles.assistantMessage
                  }`}
                  key={message.id}
                >
                  <span className={styles.messageRole}>
                    {message.role === "user" ? "You" : "Accoya knowledge"}
                  </span>
                  <div className={styles.messageContent}>{message.content}</div>
                  {message.role === "assistant" &&
                    message.sources &&
                    message.sources.length > 0 && (
                      <div className={styles.sources}>
                        <p>
                          <BookOpenText size={14} aria-hidden="true" />
                          Sources
                        </p>
                        <ul>
                          {message.sources.map((source, index) => (
                            <li key={`${source}-${index}`}>
                              {isWebUrl(source) ? (
                                <a href={source} target="_blank" rel="noreferrer">
                                  <span>{source}</span>
                                  <ExternalLink size={13} aria-hidden="true" />
                                </a>
                              ) : (
                                <span>{source}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                </article>
              ))}

              {chatMutation.isPending && (
                <div
                  className={`${styles.message} ${styles.assistantMessage}`}
                  role="status"
                >
                  <span className={styles.messageRole}>Accoya knowledge</span>
                  <div className={styles.thinking}>
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <span className={styles.screenReaderOnly}>Thinking…</span>
                  </div>
                </div>
              )}

              {chatMutation.isError && (
                <div className={styles.chatError} role="alert">
                  <div>
                    <strong>That answer couldn’t be completed.</strong>
                    <p>
                      {errorMessage(
                        chatMutation.error,
                        "Please try your question again in a moment.",
                      )}
                    </p>
                  </div>
                  {lastSubmitted && (
                    <button
                      type="button"
                      onClick={() => submitMessage(lastSubmitted, false)}
                    >
                      <RefreshCw size={15} aria-hidden="true" />
                      Try again
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        <form className={styles.composer} onSubmit={handleSubmit}>
          <label className={styles.screenReaderOnly} htmlFor="assistant-message">
            Ask the knowledge assistant
          </label>
          <textarea
            id="assistant-message"
            rows={2}
            value={input}
            placeholder="Ask about strategy, applications, or product benefits…"
            disabled={chatMutation.isPending || isRestoring}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
          />
          <button
            className={styles.sendButton}
            type="submit"
            aria-label="Send message"
            disabled={!input.trim() || chatMutation.isPending || isRestoring}
          >
            <ArrowUp size={19} aria-hidden="true" />
          </button>
          <p className={styles.composerHint}>Enter to send · Shift + Enter for a new line</p>
        </form>
      </section>
    </div>
  );
}
