import { newsViews } from '../navigation';
import { updateLocation, useNavigation } from '../../shared/navigation';
function currentView() {
  const value = new URLSearchParams(window.location.search).get('view');
  return newsViews.find(([id]) => id === value)?.[1] ?? newsViews[0][1];
}
export function useNewsView() { useNavigation(); return currentView(); }
export function changeNewsView(name: string) {
  const view = newsViews.find(([, label]) => label === name);
  if (!view || name === currentView()) return;
  const url = new URL(window.location.href);
  url.searchParams.set('view', view[0]);
  updateLocation(url);
}
