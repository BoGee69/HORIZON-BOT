"""
Static legal pages for Discord App Verification.
"""
from datetime import date
from html import escape

APP_NAME = "triadbot"
SERVICE_NAME = "TriadGames"
LAST_UPDATED = date(2026, 5, 16).strftime("%B %d, %Y")


def _page(title: str, body: str) -> str:
    safe_title = escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title} | {APP_NAME}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0d0f14;
      --panel: #151923;
      --text: #e8edf4;
      --muted: #a9b4c2;
      --border: #283041;
      --accent: #42d3b5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 16px/1.65 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    main {{
      width: min(920px, calc(100% - 32px));
      margin: 48px auto;
      padding: 32px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
    }}
    h1, h2 {{ line-height: 1.2; }}
    h1 {{ margin: 0 0 8px; font-size: 32px; }}
    h2 {{ margin-top: 32px; font-size: 20px; color: var(--accent); }}
    p, li {{ color: var(--muted); }}
    a {{ color: var(--accent); }}
    .meta {{ margin: 0 0 28px; color: var(--muted); }}
    .notice {{
      padding: 14px 16px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #10141d;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <main>
    {body}
  </main>
</body>
</html>"""


TERMS_HTML = _page(
    "Terms of Service",
    f"""
    <h1>{APP_NAME} Terms of Service</h1>
    <p class="meta">Last updated: {LAST_UPDATED}</p>
    <p class="notice">
      These Terms apply to your use of {APP_NAME}, a Discord bot operated for the {SERVICE_NAME}
      Discord community. By using the bot, you agree to follow these Terms and Discord's own
      Terms of Service, Community Guidelines, Developer Terms, and applicable laws.
    </p>

    <h2>1. Service Description</h2>
    <p>
      {APP_NAME} provides Discord commands for searching game metadata, checking availability,
      managing community access limits, and delivering download links configured by the server
      operator. The bot may include premium, donor, booster, or administrator access rules.
    </p>

    <h2>2. Eligibility and Server Rules</h2>
    <ul>
      <li>You must comply with Discord's minimum age requirements and rules.</li>
      <li>You must follow the rules of the Discord server where the bot is installed.</li>
      <li>Server staff may limit, suspend, or remove access if abuse or policy violations occur.</li>
    </ul>

    <h2>3. Acceptable Use</h2>
    <p>You agree not to use the bot to:</p>
    <ul>
      <li>Violate laws, regulations, intellectual property rights, or third-party rights.</li>
      <li>Upload, request, share, or distribute files that you do not have the legal right to use.</li>
      <li>Bypass access limits, exploit bugs, automate abuse, spam commands, or disrupt the service.</li>
      <li>Share private download links, tokens, or restricted content outside authorized channels.</li>
      <li>Harass, threaten, impersonate, or target other users or server staff.</li>
    </ul>

    <h2>4. Content and File Responsibility</h2>
    <p>
      {APP_NAME} is a tool. The server operator is responsible for configuring storage, metadata,
      permissions, and any files made available through the bot. Users and server operators must
      ensure they have all necessary rights, permissions, and licenses for any content they access,
      upload, request, or distribute.
    </p>

    <h2>5. Limits, Donor Access, and Boosters</h2>
    <p>
      Regular users may be subject to daily command limits. Donor, booster, administrator, or owner
      roles may receive expanded access. These access rules may change at any time to protect the
      service, prevent abuse, or support the community.
    </p>

    <h2>6. Payments and Donations</h2>
    <p>
      Donations, boosts, or premium access may provide community perks such as higher usage limits.
      Unless explicitly stated otherwise, donations are voluntary community support and do not
      transfer ownership of the bot, server, files, or infrastructure. Any refunds or payment issues
      are handled according to the payment platform used and the server operator's posted rules.
    </p>

    <h2>7. Availability and Changes</h2>
    <p>
      The bot is provided on an "as is" and "as available" basis. Features may be changed, rate
      limited, paused, or removed without notice. The operator does not guarantee uninterrupted
      availability, complete accuracy of metadata, or permanent access to any feature.
    </p>

    <h2>8. Enforcement</h2>
    <p>
      The server operator may deny commands, remove roles, revoke access, delete generated links,
      or ban users who violate these Terms, server rules, Discord policy, or applicable law.
    </p>

    <h2>9. Contact</h2>
    <p>
      For questions about these Terms, contact the {SERVICE_NAME} server owner or administrators
      through the official Discord server where {APP_NAME} is installed.
    </p>
    """,
)


PRIVACY_HTML = _page(
    "Privacy Policy",
    f"""
    <h1>{APP_NAME} Privacy Policy</h1>
    <p class="meta">Last updated: {LAST_UPDATED}</p>
    <p class="notice">
      This Privacy Policy explains what data {APP_NAME} processes when used in the {SERVICE_NAME}
      Discord community and how that data is used to operate the bot.
    </p>

    <h2>1. Data We Collect</h2>
    <p>The bot may process or store the following data:</p>
    <ul>
      <li>Discord user IDs, guild IDs, channel IDs, role IDs, and role names.</li>
      <li>Command usage needed to operate features, such as daily /gen usage counts.</li>
      <li>Game search queries, Steam App IDs, availability results, and generated link metadata.</li>
      <li>Operational logs, including errors, failed commands, permission issues, and abuse signals.</li>
      <li>Administrator alert data sent by DM, such as the affected user ID, guild, command, and error summary.</li>
    </ul>

    <h2>2. Data We Do Not Collect</h2>
    <ul>
      <li>We do not collect Discord passwords or authentication credentials.</li>
      <li>We do not collect payment card details. Payments, boosts, or donations are handled by their respective platforms.</li>
      <li>We do not intentionally collect private message content unrelated to bot operation.</li>
    </ul>

    <h2>3. How We Use Data</h2>
    <p>Data is used to:</p>
    <ul>
      <li>Operate bot commands and download-link delivery.</li>
      <li>Apply daily usage limits and premium, donor, booster, admin, or owner exemptions.</li>
      <li>Debug errors, detect abuse, and notify administrators about issues.</li>
      <li>Maintain service reliability, security, and server rule enforcement.</li>
    </ul>

    <h2>4. Legal and Policy Compliance</h2>
    <p>
      The bot may use Discord-provided data only as needed to provide its features and must be used
      in accordance with Discord's Terms of Service, Developer Terms, Community Guidelines, and
      applicable laws. Users and server operators are responsible for ensuring content they access
      or distribute is legally authorized.
    </p>

    <h2>5. Storage and Retention</h2>
    <p>
      Daily usage counts may be stored in a persistent server file so limits survive restarts and
      redeploys. Logs and operational data are kept only as long as reasonably needed for security,
      debugging, abuse prevention, and service operation, unless a longer period is required by law
      or server administration needs.
    </p>

    <h2>6. Third-Party Services</h2>
    <p>The bot may rely on third-party infrastructure and APIs, including:</p>
    <ul>
      <li>Discord, for bot operation, commands, roles, guild data, and user identity.</li>
      <li>Railway, for hosting and persistent service storage.</li>
      <li>Cloudflare R2, for storage configured by the server operator.</li>
      <li>Steam public APIs, for game metadata and search results.</li>
    </ul>

    <h2>7. Data Sharing</h2>
    <p>
      Data is not sold. Limited operational data may be shared with server administrators through
      admin-only commands or DM alerts when needed to fix issues, enforce rules, or protect the bot.
    </p>

    <h2>8. User Choices and Requests</h2>
    <p>
      Users may contact the {SERVICE_NAME} server owner or administrators to ask about data related
      to their bot usage, request a reset of daily usage data, or request removal where practical
      and legally appropriate.
    </p>

    <h2>9. Security</h2>
    <p>
      The operator uses reasonable technical measures such as role-based access, expiring download
      tokens, environment variables for secrets, and administrator alerts. No system is completely
      secure, and users should not share private links or restricted access.
    </p>

    <h2>10. Changes to This Policy</h2>
    <p>
      This policy may be updated as the bot changes. Continued use of the bot after updates means
      you accept the updated policy.
    </p>

    <h2>11. Contact</h2>
    <p>
      For privacy questions or requests, contact the {SERVICE_NAME} server owner or administrators
      through the official Discord server where {APP_NAME} is installed.
    </p>
    """,
)
