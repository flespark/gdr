/**
 * HTML escaping for everything the panel renders.
 *
 * Every target-provided string (task names, object names, warnings, verdict
 * details) crosses this function before it lands in markup, so a hostile name
 * cannot inject markup into the webview.
 */

const ENTITIES: Record<string, string> = {
	'&': '&amp;',
	'<': '&lt;',
	'>': '&gt;',
	'"': '&quot;',
	"'": '&#39;'
};

export function escapeHtml(text: string): string {
	return text.replace(/[&<>"']/g, (character) => ENTITIES[character]);
}
