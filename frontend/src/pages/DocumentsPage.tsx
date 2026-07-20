import { useEffect, useState } from "react";
import { api } from "../api";
import type { StrategyDoc } from "../types";

export function DocumentsPage() {
  const [docs, setDocs] = useState<StrategyDoc[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.listDocuments().then(setDocs).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);

  const onUpload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      await api.uploadDocument(file);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div>
      <div className="toolbar">
        <input
          type="file"
          disabled={uploading}
          onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
        />
        {uploading && <span className="muted">Uploading…</span>}
      </div>
      {error && <p className="error">{error}</p>}
      <ul className="list">
        {docs.map((d) => (
          <li key={d.id}>
            <strong>{d.filename}</strong>
            <span className="muted small"> — {d.s3_key}</span>
          </li>
        ))}
        {docs.length === 0 && <p className="muted">No strategy docs uploaded yet.</p>}
      </ul>
    </div>
  );
}
