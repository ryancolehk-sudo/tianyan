// Native disclosures share the same nested-Escape rule as menus and dialogs.
export function installDetailsDismissal() {
  const dismiss = (event: KeyboardEvent) => {
    if (event.key !== 'Escape' || event.defaultPrevented || !(event.target instanceof Element)) return;
    const details = event.target.closest<HTMLDetailsElement>('details[open]');
    if (!details) return;
    event.preventDefault(); event.stopPropagation(); details.open = false;
    details.querySelector<HTMLElement>(':scope > summary')?.focus({ preventScroll: true });
  };
  document.addEventListener('keydown', dismiss);
  return () => document.removeEventListener('keydown', dismiss);
}
