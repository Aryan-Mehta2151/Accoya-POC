import { useEffect, useState } from "react";
import { api } from "../api";
import type { Email, EmailStatus } from "../types";

const NEXT: Record<string, EmailStatus[]> = {
  draft: ["pending_review"],
  pending_review: ["approved", "rejected"],
  approved: ["sent"],
  sent: [],
  rejected: [],
};

export function EmailsPage() {
  const [emails, setEmails] = useState<Email[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.listEmails().then(setEmails).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);

  const changeStatus = async (id: string, status: EmailStatus) => {
    setError(null);
    try {
      await api.setEmailStatus(id, status);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      {error && <p className="error">{error}</p>}
      <div className="cards">
        {emails.map((email) => (
          <div className="card" key={email.id}>
            <span className={`badge badge-${email.status}`}>{email.status}</span>
            <h3>{email.subject ?? "(no subject)"}</h3>
            <pre>{email.body}</pre>
            <div className="actions">
              {NEXT[email.status]?.map((s) => (
                <button key={s} onClick={() => changeStatus(email.id, s)}>
                  {s === "approved"
                    ? "Approve"
                    : s === "rejected"
                      ? "Reject"
                      : s === "sent"
                        ? "Send to client"
                        : s}
                </button>
              ))}
            </div>
          </div>
        ))}
        {emails.length === 0 && (
          <p className="muted">No emails yet. Generate some from the Leads tab.</p>
        )}
      </div>
    </div>
  );
}
