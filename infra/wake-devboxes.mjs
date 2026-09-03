// Start the bot's devboxes if the platform stopped them. Run from anywhere
// with NSC_TOKEN_FILE pointing at a scoped token (devbox activate/fetch/list).
import { readFileSync } from "node:fs";
import { createDevboxClient, fromBearerToken } from "@namespacelabs/sdk/devbox";
const token = JSON.parse(readFileSync(process.env.NSC_TOKEN_FILE, "utf8")).bearer_token;
const client = createDevboxClient({ tokenSource: fromBearerToken(token) });
for (const name of (process.env.BOXES || "zulip-bot-head,fleet-workhorse").split(",")) {
  try {
    const d = await client.devboxes.get(name);
    const before = d.info.state;
    await client.devboxes.start(name);
    await d.refresh();
    console.log(new Date().toISOString(), name, "state:", before, "->", d.info.state);
  } catch (e) { console.log(new Date().toISOString(), name, "ERROR", String(e.message || e).slice(0, 200)); }
}
