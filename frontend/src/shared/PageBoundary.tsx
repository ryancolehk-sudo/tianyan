import { Component } from 'react';
import type { PropsWithChildren } from 'react';

// A failed on-demand script must leave navigation and the same dated URL usable.
export class PageBoundary extends Component<PropsWithChildren<{ context: string }>, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidUpdate(previous: Readonly<PropsWithChildren<{ context: string }>>) {
    if (this.state.failed && previous.context !== this.props.context) this.setState({ failed: false });
  }
  render() {
    return this.state.failed ? <section className="page-load-error" role="alert"><strong>页面载入失败</strong><p>请重新载入本页；当前日期与回看截止会保留。</p><button type="button" onClick={() => location.reload()}>重新载入本页</button></section> : this.props.children;
  }
}
