export type CsvValue =
  | string
  | number
  | boolean
  | Date
  | null
  | undefined
  | readonly unknown[]
  | Record<string, unknown>;

const spreadsheetFormulaPrefix = /^\s*[=+\-@]/;
const monthNumbers: Record<string, number> = {
  jan: 1,
  january: 1,
  feb: 2,
  february: 2,
  mar: 3,
  march: 3,
  apr: 4,
  april: 4,
  may: 5,
  jun: 6,
  june: 6,
  jul: 7,
  july: 7,
  aug: 8,
  august: 8,
  sep: 9,
  sept: 9,
  september: 9,
  oct: 10,
  october: 10,
  nov: 11,
  november: 11,
  dec: 12,
  december: 12,
};

function isoDateFromParts(year: number, month: number, day: number): string | null {
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > daysInMonth[month - 1]) {
    return null;
  }
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function fourDigitYear(value: string): number {
  const year = Number(value);
  if (value.length !== 2) return year;
  return year <= 68 ? 2000 + year : 1900 + year;
}

export function normalizeDateForCsv(value: string | null | undefined): string | null | undefined {
  if (value == null || !value.trim()) return value;
  const text = value.trim();
  let match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$/.exec(text);
  if (match) {
    const normalized = isoDateFromParts(Number(match[1]), Number(match[2]), Number(match[3]));
    if (normalized && (!text.slice(10) || !Number.isNaN(Date.parse(text)))) return normalized;
    return value;
  }

  match = /^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/.exec(text);
  if (match) return isoDateFromParts(fourDigitYear(match[3]), Number(match[1]), Number(match[2])) ?? value;

  match = /^(\d{4})\/(\d{1,2})\/(\d{1,2})$/.exec(text);
  if (match) return isoDateFromParts(Number(match[1]), Number(match[2]), Number(match[3])) ?? value;

  match = /^(\d{1,2})-(\d{1,2})-(\d{4})$/.exec(text);
  if (match) return isoDateFromParts(Number(match[3]), Number(match[1]), Number(match[2])) ?? value;

  match = /^(\d{1,2})-([A-Za-z]+)-(\d{4})$/.exec(text);
  if (match) {
    const month = monthNumbers[match[2].toLowerCase()];
    return month ? isoDateFromParts(Number(match[3]), month, Number(match[1])) ?? value : value;
  }

  match = /^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/.exec(text);
  if (match) {
    const month = monthNumbers[match[1].toLowerCase()];
    return month ? isoDateFromParts(Number(match[3]), month, Number(match[2])) ?? value : value;
  }
  return value;
}

function serializeArray(values: readonly unknown[]): string {
  if (values.every((value) => value == null || ['string', 'number', 'boolean'].includes(typeof value))) {
    return values
      .filter((value) => value != null)
      .map(String)
      .join('; ');
  }
  return JSON.stringify(values);
}

function serializeValue(value: CsvValue): { text: string; protectAsText: boolean } {
  if (value == null) return { text: '', protectAsText: false };
  if (value instanceof Date) {
    return {
      text: Number.isNaN(value.getTime()) ? '' : value.toISOString(),
      protectAsText: false,
    };
  }
  if (Array.isArray(value)) return { text: serializeArray(value), protectAsText: true };
  if (typeof value === 'object') return { text: JSON.stringify(value), protectAsText: true };
  return { text: String(value), protectAsText: typeof value === 'string' };
}

function escapeCell(value: CsvValue): string {
  const serialized = serializeValue(value);
  const protectedText = serialized.protectAsText && spreadsheetFormulaPrefix.test(serialized.text)
    ? `'${serialized.text}`
    : serialized.text;
  const escaped = protectedText.replaceAll('"', '""');
  return /[",\r\n]/.test(escaped) ? `"${escaped}"` : escaped;
}

export function createCsv(
  headers: readonly string[],
  rows: readonly (readonly CsvValue[])[],
): string {
  const lines = [headers.map(escapeCell).join(',')];
  rows.forEach((row) => {
    lines.push(headers.map((_, index) => escapeCell(row[index])).join(','));
  });
  return `\uFEFF${lines.join('\r\n')}\r\n`;
}

export function downloadCsv(filename: string, contents: string): void {
  const url = URL.createObjectURL(new Blob([contents], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    URL.revokeObjectURL(url);
  }
}
