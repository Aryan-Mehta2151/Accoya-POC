import { useRef, useState, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CloudUpload,
  ExternalLink,
  FileText,
  HardDrive,
  Trash2,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { StrategyDocument } from "../../types";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from "../../components/ui";
import styles from "./KnowledgePage.module.css";

function formatFileSize(bytes?: number | null) {
  if (bytes === undefined || bytes === null) return "Size unavailable";
  if (bytes === 0) return "0 B";

  const units = ["B", "KB", "MB", "GB"];
  const unitIndex = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const value = bytes / 1024 ** unitIndex;
  return `${value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

function formatDate(value?: string | null) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unavailable";

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return fallback;
}

export function KnowledgePage() {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const [isDragging, setIsDragging] = useState(false);
  const [documentToDelete, setDocumentToDelete] =
    useState<StrategyDocument | null>(null);

  const documentsQuery = useQuery({
    queryKey: queryKeys.documents,
    queryFn: api.listDocuments,
  });

  const uploadMutation = useMutation({
    mutationFn: api.uploadDocument,
    onSuccess: async (_, file) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      toast.success(`${file.name} was uploaded`);
      if (inputRef.current) inputRef.current.value = "";
    },
    onError: (error) => {
      toast.error(errorMessage(error, "The document could not be uploaded."));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (document: StrategyDocument) =>
      api.deleteDocument(document.s3_key || document.id),
    onSuccess: async (_, document) => {
      setDocumentToDelete(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.documents });
      toast.success(`${document.filename} was deleted`);
    },
    onError: (error) => {
      toast.error(errorMessage(error, "The document could not be deleted."));
    },
  });

  const uploadFile = (file?: File) => {
    if (!file || uploadMutation.isPending) return;
    uploadMutation.mutate(file);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    uploadFile(event.dataTransfer.files[0]);
  };

  const documents = documentsQuery.data ?? [];

  return (
    <div className={styles.page}>
      <PageHeader
        eyebrow="Knowledge base"
        title="Strategy library"
        description="Keep the guidance and product context that supports thoughtful outreach in one place."
      />

      <section className={styles.uploadSection} aria-labelledby="upload-title">
        <div className={styles.uploadCopy}>
          <span className={styles.iconTile} aria-hidden="true">
            <CloudUpload size={22} />
          </span>
          <div>
            <h2 id="upload-title">Add to the library</h2>
            <p>Upload a strategy document from your computer.</p>
          </div>
        </div>

        <div
          className={`${styles.dropZone} ${isDragging ? styles.dropZoneActive : ""}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node)) {
              setIsDragging(false);
            }
          }}
          onDrop={handleDrop}
        >
          <input
            ref={inputRef}
            id="strategy-document"
            className={styles.fileInput}
            type="file"
            disabled={uploadMutation.isPending}
            onChange={(event) => uploadFile(event.target.files?.[0])}
          />
          <Upload className={styles.dropIcon} size={24} aria-hidden="true" />
          <div>
            <p className={styles.dropTitle}>
              {uploadMutation.isPending
                ? "Uploading your document…"
                : "Drop a file here, or browse"}
            </p>
            <p className={styles.dropHint}>Documents are stored securely in your configured library.</p>
          </div>
          <button
            className={styles.browseButton}
            type="button"
            disabled={uploadMutation.isPending}
            onClick={() => inputRef.current?.click()}
          >
            {uploadMutation.isPending ? "Uploading…" : "Choose file"}
          </button>
        </div>

        {uploadMutation.isError && (
          <p className={styles.inlineError} role="alert">
            {errorMessage(uploadMutation.error, "The document could not be uploaded.")}
          </p>
        )}
      </section>

      <aside className={styles.indexingNotice} aria-label="Indexing status notice">
        <HardDrive size={19} aria-hidden="true" />
        <div>
          <strong>Storage and search are separate</strong>
          <p>
            Uploaded files are stored in the library. Knowledge-base indexing is not currently tracked,
            so a stored file may not be searchable immediately.
          </p>
        </div>
      </aside>

      <section className={styles.library} aria-labelledby="library-title">
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.kicker}>Your documents</p>
            <h2 id="library-title">Library</h2>
          </div>
          {!documentsQuery.isLoading && !documentsQuery.isError && (
            <span className={styles.documentCount}>
              {documents.length} {documents.length === 1 ? "document" : "documents"}
            </span>
          )}
        </div>

        {documentsQuery.isLoading && <LoadingState label="Loading strategy documents…" />}

        {documentsQuery.isError && (
          <ErrorState
            title="The library couldn’t be loaded"
            message={errorMessage(documentsQuery.error, "Please try again.")}
            onRetry={() => void documentsQuery.refetch()}
          />
        )}

        {documentsQuery.isSuccess && documents.length === 0 && (
          <EmptyState
            icon={<FileText aria-hidden="true" />}
            title="Your strategy library is ready"
            description="Upload the first document to start building your shared source of guidance."
          />
        )}

        {documents.length > 0 && (
          <ul className={styles.documentList}>
            {documents.map((document) => (
              <li className={styles.documentRow} key={document.id}>
                <div className={styles.fileMark} aria-hidden="true">
                  <FileText size={22} />
                </div>
                <div className={styles.documentDetails}>
                  <div className={styles.documentTitleRow}>
                    <h3>{document.filename}</h3>
                    <span className={styles.storedBadge}>
                      <Check size={12} aria-hidden="true" />
                      Stored
                    </span>
                  </div>
                  <p>
                    {formatFileSize(document.size)}
                    <span aria-hidden="true"> · </span>
                    <span>Added {formatDate(document.last_modified)}</span>
                  </p>
                </div>
                <div className={styles.documentActions}>
                  {document.url ? (
                    <a
                      className={styles.openButton}
                      href={document.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open
                      <ExternalLink size={15} aria-hidden="true" />
                    </a>
                  ) : (
                    <span className={styles.unavailable}>Preview unavailable</span>
                  )}
                  <button
                    className={styles.deleteButton}
                    type="button"
                    aria-label={`Delete ${document.filename}`}
                    onClick={() => setDocumentToDelete(document)}
                  >
                    <Trash2 size={17} aria-hidden="true" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmDialog
        open={documentToDelete !== null}
        onOpenChange={(open) => {
          if (!open && !deleteMutation.isPending) setDocumentToDelete(null);
        }}
        title="Delete this document?"
        description={
          documentToDelete
            ? `${documentToDelete.filename} will be removed from storage. This action cannot be undone.`
            : "This document will be removed from storage."
        }
        confirmLabel="Delete document"
        onConfirm={() => {
          if (documentToDelete) deleteMutation.mutate(documentToDelete);
        }}
        pending={deleteMutation.isPending}
        variant="danger"
      />
    </div>
  );
}
