/**
 * The panel's view state: what the webview shows, owned by the host.
 *
 * The host renders this state to HTML (see `render.ts`) and ships the markup
 * over; the webview never holds target data of its own, so a session switch or
 * refresh is purely host-side state churn.
 */

import type { PanelSnapshot } from '../snapshot.ts';

export type SessionStatus = 'waiting' | 'running' | 'ok' | 'error';

export interface PanelSessionState {
	id: string;
	name: string;
	status: SessionStatus;
	/** Why the session is waiting or failed, for the status block. */
	message: string | null;
	/** The last applied snapshot; kept while the target runs. */
	snapshot?: PanelSnapshot;
}

export interface PanelState {
	sessions: PanelSessionState[];
	activeId: string | null;
}
