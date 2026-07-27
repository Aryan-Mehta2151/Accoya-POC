// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import { AssistantPage } from "./AssistantPage";

vi.mock("../../lib/api", () => {
  class MockApiError extends Error {}

  return {
    ApiError: MockApiError,
    api: {
      chat: vi.fn(),
      deleteChat: vi.fn(),
      getChatHistory: vi.fn(),
      listChatSessions: vi.fn(),
    },
  };
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return {
    user: userEvent.setup(),
    ...render(
      <QueryClientProvider client={queryClient}>
        <AssistantPage />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: true }),
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: vi.fn(),
  });
  vi.mocked(api.getChatHistory).mockResolvedValue([]);
  vi.mocked(api.listChatSessions).mockResolvedValue([]);
  vi.mocked(api.deleteChat).mockResolvedValue({
    deleted: true,
    session_id: "deleted-session",
  });
});

afterEach(() => cleanup());

describe("AssistantPage", () => {
  it("restores a known conversation from session storage", async () => {
    window.sessionStorage.setItem(
      "accoya-outreach-assistant-session",
      "saved-session",
    );
    vi.mocked(api.getChatHistory).mockResolvedValue([
      { role: "user", content: "What matters most?" },
      { role: "assistant", content: "Long-term performance and stability." },
    ]);
    renderPage();

    expect(await screen.findByText("What matters most?")).toBeInTheDocument();
    expect(screen.getByText("Long-term performance and stability.")).toBeInTheDocument();
    expect(api.getChatHistory).toHaveBeenCalledWith("saved-session");
  });

  it("sends on Enter, preserves Shift+Enter, and renders safe source links", async () => {
    vi.mocked(api.chat).mockResolvedValue({
      session_id: "new-session",
      answer: "Use the durability and sustainability evidence.",
      sources: ["Internal strategy brief", "https://example.com/evidence"],
    });
    const { user } = renderPage();
    const composer = screen.getByRole("textbox", {
      name: "Ask the knowledge assistant",
    });

    await user.type(composer, "First line{Shift>}{Enter}{/Shift}Second line");
    expect(api.chat).not.toHaveBeenCalled();
    expect(composer).toHaveValue("First line\nSecond line");

    await user.type(composer, "{Enter}");
    expect(api.chat).toHaveBeenCalledWith("First line\nSecond line", null);
    expect(
      await screen.findByText("Use the durability and sustainability evidence."),
    ).toBeInTheDocument();
    expect(screen.getByText("Internal strategy brief").closest("a")).toBeNull();
    expect(screen.getByRole("link", { name: /example.com\/evidence/i })).toHaveAttribute(
      "href",
      "https://example.com/evidence",
    );
    expect(window.sessionStorage.getItem("accoya-outreach-assistant-session")).toBe(
      "new-session",
    );
  });

  it("shows an accessible failure and retries without duplicating the question", async () => {
    vi.mocked(api.chat).mockRejectedValueOnce(new Error("Assistant unavailable"));
    const { user } = renderPage();
    const composer = screen.getByRole("textbox", {
      name: "Ask the knowledge assistant",
    });

    await user.type(composer, "How should I prepare?{Enter}");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Assistant unavailable");
    expect(screen.getAllByText("How should I prepare?")).toHaveLength(1);

    vi.mocked(api.chat).mockResolvedValueOnce({
      session_id: "retry-session",
      answer: "Lead with the application requirements.",
      sources: [],
    });
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Lead with the application requirements.")).toBeInTheDocument();
    expect(screen.getAllByText("How should I prepare?")).toHaveLength(1);
    expect(api.chat).toHaveBeenCalledTimes(2);
  });

  it("starts a clean conversation and sends its first question without the old session", async () => {
    window.sessionStorage.setItem(
      "accoya-outreach-assistant-session",
      "saved-session",
    );
    vi.mocked(api.getChatHistory).mockResolvedValue([
      { role: "assistant", content: "An earlier answer." },
    ]);
    const { user } = renderPage();

    expect(await screen.findByText("An earlier answer.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New conversation" }));

    expect(screen.queryByText("An earlier answer.")).not.toBeInTheDocument();
    expect(screen.getByText("What would you like to understand?")).toBeInTheDocument();
    expect(window.sessionStorage.getItem("accoya-outreach-assistant-session")).toBeNull();

    vi.mocked(api.chat).mockResolvedValue({
      session_id: "replacement-session",
      answer: "A clean answer.",
      sources: [],
    });
    await user.type(
      screen.getByRole("textbox", { name: "Ask the knowledge assistant" }),
      "Start fresh{Enter}",
    );

    expect(api.chat).toHaveBeenCalledWith("Start fresh", null);
    expect(window.sessionStorage.getItem("accoya-outreach-assistant-session")).toBe(
      "replacement-session",
    );
  });

  it("keeps a new conversation empty when the old history request finishes late", async () => {
    window.sessionStorage.setItem(
      "accoya-outreach-assistant-session",
      "slow-session",
    );
    let resolveHistory!: (messages: Array<{
      role: "user" | "assistant";
      content: string;
    }>) => void;
    vi.mocked(api.getChatHistory).mockReturnValue(
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
    );
    const { user } = renderPage();

    await user.click(
      screen.getByRole("button", { name: "Start new conversation" }),
    );
    expect(screen.getByText("What would you like to understand?")).toBeInTheDocument();

    resolveHistory([{ role: "assistant", content: "A late old answer." }]);

    expect(await screen.findByText("What would you like to understand?")).toBeInTheDocument();
    expect(screen.queryByText("A late old answer.")).not.toBeInTheDocument();
  });

  it("switches between saved conversations without mixing transcripts", async () => {
    window.sessionStorage.setItem(
      "accoya-outreach-assistant-session",
      "first-session",
    );
    vi.mocked(api.listChatSessions).mockResolvedValue([
      {
        session_id: "first-session",
        message_count: 2,
        last_message_at: "2026-07-27T12:00:00Z",
      },
      {
        session_id: "second-session",
        message_count: 2,
        last_message_at: "2026-07-26T12:00:00Z",
      },
    ]);
    vi.mocked(api.getChatHistory).mockImplementation(async (sessionId) =>
      sessionId === "first-session"
        ? [{ role: "assistant", content: "First transcript." }]
        : [{ role: "assistant", content: "Second transcript." }],
    );
    const { user } = renderPage();

    expect(await screen.findByText("First transcript.")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /Chat 2/ }));

    expect(await screen.findByText("Second transcript.")).toBeInTheDocument();
    expect(screen.queryByText("First transcript.")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("accoya-outreach-assistant-session")).toBe(
      "second-session",
    );
  });
});
