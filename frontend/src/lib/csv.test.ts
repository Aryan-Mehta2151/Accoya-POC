// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { createCsv, downloadCsv, normalizeDateForCsv } from './csv';

const originalCreateObjectUrl = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

describe('CSV utilities', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    if (originalCreateObjectUrl) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectUrl);
    else Reflect.deleteProperty(URL, 'createObjectURL');
    if (originalRevokeObjectUrl) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectUrl);
    else Reflect.deleteProperty(URL, 'revokeObjectURL');
  });

  it('writes UTF-8 CSV with CRLF rows and safely serializes supported values', () => {
    const csv = createCsv(
      ['Name', 'Notes', 'Amount', 'Created', 'Tags', 'Details', 'Empty'],
      [[
        'Zoë, Inc.',
        'First line\nHe said "hello"',
        -12.5,
        new Date('2026-09-27T12:34:56Z'),
        ['cladding', 'public realm'],
        { source: 'agenda', count: 2 },
        null,
      ]],
    );

    expect(csv.startsWith('\uFEFF')).toBe(true);
    expect(csv).toBe(
      '\uFEFFName,Notes,Amount,Created,Tags,Details,Empty\r\n'
      + '"Zoë, Inc.","First line\nHe said ""hello""",-12.5,2026-09-27T12:34:56.000Z,'
      + 'cladding; public realm,"{""source"":""agenda"",""count"":2}",\r\n',
    );
  });

  it('neutralizes formula-like text without changing numeric values', () => {
    const csv = createCsv(
      ['Formula', 'Whitespace', 'At', 'Positive', 'Number'],
      [['=2+2', '  -10', '@command', '+SUM(1,2)', -10]],
    );

    expect(csv).toContain("'=2+2,'  -10,'@command,\"'+SUM(1,2)\",-10");
  });

  it('normalizes supported source dates and preserves invalid text', () => {
    expect(normalizeDateForCsv('Jul 17, 2026')).toBe('2026-07-17');
    expect(normalizeDateForCsv('APRIL 10, 2026')).toBe('2026-04-10');
    expect(normalizeDateForCsv('07/04/26')).toBe('2026-07-04');
    expect(normalizeDateForCsv('17-Jul-2026')).toBe('2026-07-17');
    expect(normalizeDateForCsv('2026-07-17T23:30:00-07:00')).toBe('2026-07-17');
    expect(normalizeDateForCsv('2026-02-30')).toBe('2026-02-30');
    expect(normalizeDateForCsv('Waiting on owner')).toBe('Waiting on owner');
    expect(normalizeDateForCsv(null)).toBeNull();
  });

  it('downloads a blob and always releases its object URL', () => {
    const click = vi.fn();
    const remove = vi.fn();
    const link = { href: '', download: '', hidden: false, click, remove } as unknown as HTMLAnchorElement;
    vi.spyOn(document, 'createElement').mockReturnValue(link);
    vi.spyOn(document.body, 'appendChild').mockImplementation((node) => node);
    const createObjectURL = vi.fn(() => 'blob:csv-test');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });

    downloadCsv('opportunities.csv', '\uFEFFName\r\nAccoya\r\n');

    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(link.download).toBe('opportunities.csv');
    expect(link.href).toBe('blob:csv-test');
    expect(click).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:csv-test');
  });
});
