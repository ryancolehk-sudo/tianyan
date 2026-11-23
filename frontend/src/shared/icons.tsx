import type { SVGProps } from 'react';

const paths = {
  workbench: <><path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/></>,
  auction: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,
  news: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h4M7 16h10m-3-4h3"/></>,
  research: <><path d="M15 4H4v17h16V10M8 14l1-4L19 1l4 4-10 9Z"/><path d="m16 4 4 4"/></>,
  data: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/></>,
  system: <><path d="m9 3 1-2h4l1 2 3 2 3 1v4l-2 2 2 2v4l-3 1-3 2-1 2h-4l-1-2-3-2-3-1v-4l2-2-2-2V6l3-1Z"/><circle cx="12" cy="12" r="3"/></>,
  search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,
  refresh: <><path d="M20 7a9 9 0 1 0 1 9M20 2v6h-6"/></>,
  logout: <><path d="M10 3H4v18h6m5-14 5 5-5 5m-7-5h12"/></>,
  menu: <path d="M3 6h18M3 12h18M3 18h18"/>,
  close: <path d="m5 5 14 14M5 19 19 5"/>,
  arrow: <path d="M3 12h18m-6-6 6 6-6 6"/>,
  chevron: <path d="m6 9 6 6 6-6"/>,
  chart: <><path d="M4 3v17h17"/><path d="m7 15 4-5 4 2 5-7"/></>,
  shield: <><path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z"/><path d="M12 7v6m0 3h.01"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/></>,
  plus: <path d="M12 4v16M4 12h16"/>,
  drag: <><circle cx="8" cy="5" r="1"/><circle cx="16" cy="5" r="1"/><circle cx="8" cy="12" r="1"/><circle cx="16" cy="12" r="1"/><circle cx="8" cy="19" r="1"/><circle cx="16" cy="19" r="1"/></>,
  resize: <path d="m8 20 12-12m-5 12 5-5"/>,
} as const;

export type IconName = keyof typeof paths;
export function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>;
}
