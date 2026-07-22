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
      getChatHistory: vi.fn(),
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

  it("starts a clean conversation and removes the stored session", async () => {
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
  });
});
