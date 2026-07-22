export function formatDate(value?: string | null, options?: Intl.DateTimeFormatOptions) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en-US', options ?? {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }).format(date);
}

export function formatFileSize(bytes?: number) {
  if (bytes == null) return 'Size unavailable';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let size = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${unit}`;
}

export function formatLocation(location?: string | null, state?: string | null) {
  return [location, state].filter(Boolean).join(', ') || 'Location unavailable';
}

export function formatScore(score?: number | null) {
  return score == null ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(score);
}
