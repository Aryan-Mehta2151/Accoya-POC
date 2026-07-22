// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import type { StrategyDocument } from "../../types";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../../lib/api", () => {
  class MockApiError extends Error {}

  return {
    ApiError: MockApiError,
    api: {
      listDocuments: vi.fn(),
      uploadDocument: vi.fn(),
      deleteDocument: vi.fn(),
    },
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const document: StrategyDocument = {
  id: "strategy/brief.pdf",
  s3_key: "strategy/brief.pdf",
  filename: "Accoya strategy brief.pdf",
  size: 1536,
  last_modified: "2026-07-20T10:00:00Z",
  url: "https://files.example.com/brief.pdf",
};

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
        <KnowledgePage />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listDocuments).mockResolvedValue([document]);
});

afterEach(() => cleanup());

describe("KnowledgePage", () => {
  it("presents stored document metadata without claiming it is indexed", async () => {
    renderPage();

    expect(await screen.findByText(document.filename)).toBeInTheDocument();
    expect(screen.getByText("1.5 KB", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Stored")).toBeInTheDocument();
    expect(screen.getByText("Storage and search are separate")).toBeInTheDocument();
    expect(screen.getByText(/indexing is not currently tracked/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open/i })).toHaveAttribute(
      "href",
      document.url,
    );
  });

  it("uploads a document dropped onto the upload surface", async () => {
    vi.mocked(api.uploadDocument).mockResolvedValue(document);
    const file = new File(["strategy"], "new-strategy.pdf", {
      type: "application/pdf",
    });
    renderPage();

    const dropCopy = screen.getByText("Drop a file here, or browse");
    const dropZone = dropCopy.parentElement?.parentElement;
    expect(dropZone).not.toBeNull();
    fireEvent.drop(dropZone as HTMLElement, {
      dataTransfer: { files: [file] },
    });

    await waitFor(() =>
      expect(api.uploadDocument).toHaveBeenCalledWith(file, expect.anything()),
    );
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(2));
  });

  it("requires confirmation before deleting a document", async () => {
    vi.mocked(api.deleteDocument).mockResolvedValue({
      deleted: true,
      s3_key: document.s3_key,
    });
    const { user } = renderPage();

    await user.click(
      await screen.findByRole("button", { name: `Delete ${document.filename}` }),
    );
    expect(screen.getByRole("dialog")).toHaveTextContent("Delete this document?");
    expect(api.deleteDocument).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete document" }));
    await waitFor(() =>
      expect(api.deleteDocument).toHaveBeenCalledWith(document.s3_key),
    );
  });
});
