export type LeaderEvidence = {
  rows: { stock_code: string; name: string; label: string; rank: string }[];
  coverage: string;
  received_at?: string | null;
  archive_kind?: string | null;
};

const shanghaiClock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function receiptDate(value: string | null | undefined): Date | null {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction, zone, zoneHour, zoneMinute] = match;
  const y = Number(year), m = Number(month), d = Number(day);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (y < 1 || m < 1 || m > 12 || d < 1 || d > monthDays[m - 1] || Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59 || Number(zoneHour || 0) > 23 || Number(zoneMinute || 0) > 59) return null;
  const milliseconds = (fraction || '').padEnd(3, '0').slice(0, 3);
  const parsed = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}.${milliseconds}${zone}`);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

export function describeLeaderEvidence(leader: LeaderEvidence, requestedDay: string | null | undefined) {
  const coverageNote = leader.coverage === 'complete' ? '' : leader.coverage === 'returned_markers' ? '部分标记' : '覆盖未知';
  const archive = leader.archive_kind === 'daily' ? '日终归档' : leader.archive_kind === 'intraday' ? '盘中观察' : '归档时段未知';
  const receipt = receiptDate(leader.received_at);
  if (!receipt) return { receiptLabel: '接收未知', receiptISO: undefined, title: `领涨详情 · ${archive} · 接收时间未知`, coverageNote };
  const parts = Object.fromEntries(shanghaiClock.formatToParts(receipt).map(part => [part.type, part.value]));
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  const time = `${parts.hour}:${parts.minute}:${parts.second}`;
  const beforeOpen = leader.archive_kind === 'intraday' && date === requestedDay && time < '09:30:00';
  return {
    receiptLabel: `接收${date === requestedDay ? time.slice(0, 5) : date.slice(5)}`,
    receiptISO: receipt.toISOString(),
    title: `领涨详情 · ${archive} · 接收 ${date} ${time}（北京时间）${beforeOpen ? ' · 盘前接收' : ''}`,
    coverageNote,
  };
}
