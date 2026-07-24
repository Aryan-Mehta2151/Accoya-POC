// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import type { Email, Lead } from "../../types";
import { OutreachDetailPage } from "./OutreachDetailPage";
import { OutreachPage } from "./OutreachPage";

vi.mock("../../lib/api", () => {
  class MockApiError extends Error {}

  return {
    ApiError: MockApiError,
    api: {
      listEmails: vi.fn(),
      listLeads: vi.fn(),
      editEmail: vi.fn(),
      setEmailStatus: vi.fn(),
    },
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const lead: Lead = {
  id: "lead-1",
  external_id: "external-1",
  section: "Commercial",
  project: "Harbour Arts Centre",
  location: "Portland",
  state: "OR",
  signal: "Specification",
  intelligence: null,
  score: 91,
  timing: "Q4",
  awarded_to: null,
  priority_reasons: null,
  summary: "A premium exterior timber opportunity.",
  contacts: "Maya Chen",
  contact_email: "maya@example.com",
  meeting_date: null,
  tags: "cultural, facade",
  url: null,
  source_feed: "earlybid",
  created_at: "2026-07-20T10:00:00Z",
};

function email(status: Email["status"], id = `email-${status}`): Email {
  return {
    id,
    lead_id: lead.id,
    subject: `${status} proposal`,
    body: "Hello Maya,\n\nHere is a thoughtful project note.",
    status,
    created_at: "2026-07-20T11:00:00Z",
    updated_at: "2026-07-21T11:00:00Z",
  };
}

function renderRoute(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const router = createMemoryRouter(
    [
      { path: "/outreach", element: <OutreachPage /> },
      { path: "/outreach/:emailId", element: <OutreachDetailPage /> },
      { path: "/opportunities", element: <div>Opportunities</div> },
    ],
    { initialEntries: [path] },
  );

  return {
    router,
    user: userEvent.setup(),
    ...render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listLeads).mockResolvedValue([lead]);
});

afterEach(() => cleanup());

describe("OutreachPage", () => {
  it("shows status counts and searches across joined lead details", async () => {
    vi.mocked(api.listEmails).mockResolvedValue([
      email("pending_review"),
      email("approved"),
      email("sent"),
    ]);
    const { user } = renderRoute("/outreach");

    expect(await screen.findByRole("heading", { name: "Outrech overview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Needs review\s*1/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("pending_review proposal")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search outreach" }), "approved");
    expect(screen.getByRole("button", { name: /All\s*3/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("approved proposal")).toBeInTheDocument();
    expect(screen.queryByText("pending_review proposal")).not.toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Search outreach" }));
    await user.type(screen.getByRole("searchbox", { name: "Search outreach" }), "no match");
    expect(screen.getByRole("heading", { name: "No matching messages" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    await user.click(screen.getByRole("button", { name: /Approved\s*1/i }));
    expect(screen.getByText("approved proposal")).toBeInTheDocument();
  });
});

describe("OutreachDetailPage", () => {
  it("saves edits explicitly before enabling review actions", async () => {
    const draft = email("pending_review", "email-1");
    const updated = { ...draft, subject: "A considered Accoya proposal" };
    vi.mocked(api.listEmails).mockResolvedValue([draft]);
    vi.mocked(api.editEmail).mockResolvedValue(updated);
    vi.mocked(api.setEmailStatus).mockImplementation(async (_id, status) => ({
      ...updated,
      status,
    }));
    const { user } = renderRoute("/outreach/email-1");

    const subject = await screen.findByRole("textbox", { name: "Subject" });
    await user.clear(subject);
    await user.type(subject, updated.subject);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(api.editEmail).toHaveBeenCalledWith("email-1", {
        subject: updated.subject,
        body: draft.body,
      });
    });

    const approve = screen.getByRole("button", { name: "Approve" });
    await waitFor(() => expect(approve).toBeEnabled());
    await user.click(approve);
    await waitFor(() => expect(api.setEmailStatus).toHaveBeenCalledWith("email-1", "approved"));
  });

  it("warns before leaving with unsaved changes", async () => {
    const draft = email("draft", "email-1");
    vi.mocked(api.listEmails).mockResolvedValue([draft]);
    const { user } = renderRoute("/outreach/email-1");

    await user.type(await screen.findByRole("textbox", { name: "Subject" }), " updated");
    await user.click(screen.getByRole("link", { name: "Back to outreach" }));

    expect(screen.getByRole("dialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("textbox", { name: "Subject" })).toHaveValue(`${draft.subject} updated`);
  });

  it("explains that Mark as sent records status but does not deliver mail", async () => {
    const approved = email("approved", "email-1");
    vi.mocked(api.listEmails).mockResolvedValue([approved]);
    vi.mocked(api.setEmailStatus).mockResolvedValue({ ...approved, status: "sent" });
    const { user } = renderRoute("/outreach/email-1");

    await user.click(await screen.findByRole("button", { name: "Mark as sent" }));
    expect(
      screen.getByText(/does not deliver the email to the recipient/i),
    ).toBeInTheDocument();
    expect(api.setEmailStatus).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog", { name: "Mark this outreach as sent?" });
    await user.click(within(dialog).getByRole("button", { name: "Mark as sent" }));
    await waitFor(() => expect(api.setEmailStatus).toHaveBeenCalledWith("email-1", "sent"));
  });

  it("keeps terminal messages read-only", async () => {
    const sent = email("sent", "email-1");
    vi.mocked(api.listEmails).mockResolvedValue([sent]);
    renderRoute("/outreach/email-1");

    expect(await screen.findByRole("textbox", { name: "Subject" })).toHaveAttribute("readonly");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });
});
