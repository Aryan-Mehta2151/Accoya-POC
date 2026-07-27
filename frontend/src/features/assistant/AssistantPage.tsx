import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  BookOpenText,
  ExternalLink,
  MessageCircle,
  MessageCircleMore,
  Plus,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Trash2,
} from "lucide-react";

import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { ChatMessage, ChatSession } from "../../types";
import { ErrorState, LoadingState, PageHeader } from "../../components/ui";
import styles from "./AssistantPage.module.css";

const SESSION_STORAGE_KEY = "accoya-outreach-assistant-session";

const SUGGESTED_PROMPTS = [
  "What should we emphasize when introducing Accoya to an architect?",
  "Summarize the strongest sustainability talking points.",
  "Which product benefits matter most for exterior applications?",
  "Help me prepare for a conversation about long-term performance.",
];

type ConversationMessage = ChatMessage & { id: string; isNew?: boolean };

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

function timeAgo(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function sessionLabel(_session: ChatSession, index: number): string {
  return `Chat ${index + 1}`;
}

function parseInline(text: string, keyPrefix: string): ReactNode[] {
  /**Convert inline markdown: **bold**, *italic*, `code`. */
  const parts: ReactNode[] = [];
  // Match **bold**, *italic*, or `code`
  const regex = /(\*\*([^*]+)\*\*)|(\*([^*]+)\*)|(`([^`]+)`)/g;
  let lastIndex = 0;
  let match;
  let idx = 0;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    if (match[2] !== undefined) {
      parts.push(<strong key={`${keyPrefix}-b-${idx}`}>{match[2]}</strong>);
    } else if (match[4] !== undefined) {
      parts.push(<em key={`${keyPrefix}-i-${idx}`}>{match[4]}</em>);
    } else if (match[6] !== undefined) {
      parts.push(<code key={`${keyPrefix}-c-${idx}`}>{match[6]}</code>);
    }
    lastIndex = match.index + match[0].length;
    idx += 1;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts.length > 0 ? parts : [text];
}

function parseMarkdown(text: string): ReactNode {
  /**Render block-level markdown: headings, bullets, rules, paragraphs. */
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let listItems: ReactNode[] = [];
  let key = 0;

  const flushList = () => {
    if (listItems.length > 0) {
      blocks.push(
        <ul key={`ul-${key++}`} className={styles.mdList}>
          {listItems}
        </ul>,
      );
      listItems = [];
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushList();
      blocks.push(<hr key={`hr-${key++}`} className={styles.mdRule} />);
      continue;
    }

    // Headings ### ## #
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushList();
      const level = heading[1].length;
      const content = parseInline(heading[2], `h-${key}`);
      const Tag = `h${Math.min(level + 2, 6)}` as keyof React.JSX.IntrinsicElements;
      blocks.push(
        <Tag key={`h-${key++}`} className={styles.mdHeading}>
          {content}
        </Tag>,
      );
      continue;
    }

    // Bullet list items - * or -
    const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      listItems.push(
        <li key={`li-${key++}`}>{parseInline(bullet[1], `li-${key}`)}</li>,
      );
      continue;
    }

    // Numbered list items 1. 2.
    const numbered = /^\d+\.\s+(.*)$/.exec(trimmed);
    if (numbered) {
      listItems.push(
        <li key={`li-${key++}`}>{parseInline(numbered[1], `li-${key}`)}</li>,
      );
      continue;
    }

    // Blank line ends a list
    if (trimmed === "") {
      flushList();
      continue;
    }

    // Regular paragraph line
    flushList();
    blocks.push(
      <p key={`p-${key++}`} className={styles.mdParagraph}>
        {parseInline(line, `p-${key}`)}
      </p>,
    );
  }

  flushList();
  return <>{blocks}</>;
}

type TypewriterTextProps = {
  messageId: string;
  text: string;
  onDone?: (messageId: string) => void;
  onTick?: () => void;
};

function TypewriterText({
  messageId,
  text,
  onDone,
  onTick,
}: TypewriterTextProps) {
  /**Reveal text word by word like ChatGPT streaming. */
  const words = useMemo(() => text.split(/(\s+)/), [text]);
  const [wordCount, setWordCount] = useState(() =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? words.length
      : 0,
  );

  useEffect(() => {
    if (wordCount >= words.length) {
      onDone?.(messageId);
      return;
    }
    const timer = setTimeout(() => {
      setWordCount((count) => count + 1);
      onTick?.();
    }, 40);
    return () => clearTimeout(timer);
  }, [messageId, onDone, onTick, wordCount, words.length]);

  const shown = words.slice(0, wordCount).join("");
  return <>{parseMarkdown(shown)}</>;
}

export function AssistantPage() {
  const queryClient = useQueryClient();
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [sessionId, setSessionId] = useState<string | null>(storedSessionId);
  const [localMessages, setLocalMessages] = useState<
    ConversationMessage[] | null
  >(() => (storedSessionId() ? null : []));
  const [input, setInput] = useState("");
  const [lastSubmitted, setLastSubmitted] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<{ sessionId: string } | null>(null);

  const sessionsQuery = useQuery({
    queryKey: queryKeys.chatSessions,
    queryFn: () => api.listChatSessions(),
    refetchOnWindowFocus: false,
  });

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
        isNew: false,
      })),
    [historyQuery.data],
  );
  const messages = localMessages ?? historyMessages;
  const isRestoring =
    Boolean(sessionId) && localMessages === null && historyQuery.isLoading;

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
        ...(current ?? historyMessages),
        {
          id: `assistant-${Date.now()}`,
          role: "assistant",
          content: response.answer,
          sources: response.sources,
          isNew: true,
        },
      ]);
      setLastSubmitted(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.chatSessions });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteChat(id),
    onSuccess: (_response, deletedId) => {
      if (deletedId === sessionId) {
        setSessionId(null);
        setLocalMessages([]);
        saveSessionId(null);
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.chatSessions });
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

  const markMessageDone = useCallback((id: string) => {
    setLocalMessages((current) =>
      current?.map((m) => (m.id === id ? { ...m, isNew: false } : m)) ??
      current,
    );
  }, []);

  const scrollToBottom = useCallback(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    transcript.scrollTop = transcript.scrollHeight;
  }, []);

  const submitMessage = (text: string, appendUser = true) => {
    const trimmed = text.trim();
    if (!trimmed || chatMutation.isPending || isRestoring) return;

    chatMutation.reset();
    setLastSubmitted(trimmed);
    if (appendUser) {
      setLocalMessages((current) => [
        ...(current ?? historyMessages),
        {
          id: `user-${Date.now()}`,
          role: "user",
          content: trimmed,
          isNew: false,
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

  const switchToSession = (id: string) => {
    if (id === sessionId) return;
    chatMutation.reset();
    setLocalMessages(null);
    setInput("");
    setLastSubmitted(null);
    setSessionId(id);
    saveSessionId(id);
  };

  const deleteSession = (id: string, event: React.MouseEvent) => {
    event.stopPropagation();
    setDeleteConfirm({ sessionId: id });
  };

  const confirmDelete = () => {
    if (deleteConfirm) {
      deleteMutation.mutate(deleteConfirm.sessionId);
      setDeleteConfirm(null);
    }
  };

  const cancelDelete = () => {
    setDeleteConfirm(null);
  };

  const hasMessages = messages.length > 0;
  const sessions = sessionsQuery.data ?? [];

  return (
    <div className={styles.page}>
      <PageHeader
        eyebrow="Knowledge assistant"
        title="Ask with confidence"
        description="Explore the strategy and product knowledge available to your team."
      />

      <div className={styles.assistantLayout}>
        {/* Sessions sidebar */}
        <aside className={styles.sidebar}>
          <div className={styles.sidebarHeader}>
            <span>Conversations</span>
            <button
              className={styles.newChatButton}
              type="button"
              title="New conversation"
              aria-label="Start new conversation"
              onClick={startNewConversation}
              disabled={chatMutation.isPending || (!sessionId && !hasMessages)}
            >
              <Plus size={15} aria-hidden="true" />
            </button>
          </div>

          <div className={styles.sessionList}>
            {/* Active unsaved session (no sessionId yet) */}
            {!sessionId && hasMessages && (
              <button className={`${styles.sessionItem} ${styles.sessionItemActive}`} type="button" disabled>
                <MessageCircle size={14} aria-hidden="true" />
                <span className={styles.sessionItemLabel}>New chat</span>
                <span className={styles.sessionItemTime}>now</span>
              </button>
            )}

            {sessions.length === 0 && !sessionsQuery.isLoading && sessionId === null && !hasMessages && (
              <p className={styles.noSessions}>No conversations yet</p>
            )}

            {sessions.map((session, index) => (
              <div
                key={session.session_id}
                className={`${styles.sessionItem} ${session.session_id === sessionId ? styles.sessionItemActive : ""}`}
              >
                <button
                  className={styles.sessionItemButton}
                  type="button"
                  onClick={() => switchToSession(session.session_id)}
                  style={{ flex: 1, display: "grid", gridTemplateColumns: "auto 1fr auto", gap: "8px", alignItems: "center", cursor: "pointer" }}
                >
                  <MessageCircle size={14} aria-hidden="true" />
                  <span className={styles.sessionItemLabel}>{sessionLabel(session, index)}</span>
                  <span className={styles.sessionItemTime}>{timeAgo(session.last_message_at)}</span>
                </button>
                <button
                  type="button"
                  className={styles.sessionItemDeleteButton}
                  onClick={(e) => deleteSession(session.session_id, e)}
                  aria-label="Delete session"
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        </aside>

        {/* Chat panel */}
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
            <button
              className={styles.newConversationButton}
              type="button"
              disabled={chatMutation.isPending || (!sessionId && !hasMessages)}
              onClick={startNewConversation}
            >
              <RotateCcw size={16} aria-hidden="true" />
              New conversation
            </button>
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
                  title="We couldn't restore this conversation"
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
                    <div className={styles.messageContent}>
                      {message.role === "assistant" && message.isNew ? (
                        <TypewriterText
                          key={message.id}
                          messageId={message.id}
                          text={message.content}
                          onDone={markMessageDone}
                          onTick={scrollToBottom}
                        />
                      ) : (
                        parseMarkdown(message.content)
                      )}
                    </div>
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
                      <strong>That answer couldn't be completed.</strong>
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

          {/* Delete confirmation modal */}
          {deleteConfirm && (
            <div className={styles.confirmationOverlay} onClick={cancelDelete}>
              <div className={styles.confirmationModal} onClick={(e) => e.stopPropagation()}>
                <h3>Delete conversation?</h3>
                <p>This action cannot be undone.</p>
                <div className={styles.confirmationButtons}>
                  <button
                    type="button"
                    className={styles.cancelButton}
                    onClick={cancelDelete}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className={styles.deleteButton}
                    onClick={confirmDelete}
                    disabled={deleteMutation.isPending}
                  >
                    {deleteMutation.isPending ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
