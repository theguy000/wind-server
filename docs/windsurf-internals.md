# Windsurf IDE - Authentication & Telemetry Knowledge Base

Based on network traffic analysis (via Fiddler and mitmproxy), this document outlines how Windsurf manages authentication, tracks user activity, and enforces quotas.

## 1. Proxy Configuration
To intercept Windsurf traffic, proxy settings must be applied in multiple places because different internal components (Electron, language servers, extensions) handle routing differently.

*   **Core Process (`~/.windsurf/argv.json`):**
    Requires the proxy flag to force the main process to use it.
    ```json
    "proxy-server": "http://127.0.0.1:8080"
    ```
*   **User Settings (`~/.config/Windsurf/User/settings.json`):**
    ```json
    "http.proxy": "http://127.0.0.1:8080",
    "http.proxySupport": "on",
    "http.proxyStrictSSL": false,
    "windsurf.proxySupport": "on",
    "windsurf.http.proxy": "http://127.0.0.1:8080",
    "windsurf.http.proxyStrictSSL": false
    ```
*   **Certificate Trust:**
    The proxy's Root CA (Fiddler or mitmproxy) must be trusted by the OS. Additionally, setting `export NODE_EXTRA_CA_CERTS="/path/to/proxy_root.crt"` in `~/.bashrc` ensures internal Node.js processes trust the connection.

## 2. Authentication & Identity
Windsurf uses a combination of tokens and hardware IDs to track the user across services.

### Account Metadata (Extracted from `GetUserStatus`):
*   **User Name:** Istiak Mahmud
*   **Email:** istia.k.m30@gmail.com
*   **Team ID:** `devin-team$account-fd09f6761331492ca3ebd62f99eb3673`

### Key Identifiers:
1.  **Devin Session Token (JWT):**
    Used heavily in API requests. Contains a `session_id` payload.
    *Example Format:* `devin-session-token$eyJhbGciOiJI...`
2.  **Authorization Header:**
    The primary token for AI requests, usually formatted as `*:production.<hash>`.
3.  **Machine / Installation IDs:**
    *   `installationId`: Uniquely identifies the local install.
    *   `Unleash-Instanceid`: Identifies the machine name (e.g., `istiak-b550aoruselitev2`).

## 3. How "Charging" and Quotas Work
Windsurf operates on a credit-based system. Usage is tracked per "message" or per "agent step".

### Trial Plan Quotas & Live Tracking:
*   **Static Limits:** The plan allocates baseline limits (e.g., 16384 premium/600 standard tokens or limits), but the actual enforcement is dynamic.
*   **Live Percentage Tracking:** The server sends back exact decreasing integers representing your remaining quotas.
    *   A byte decreasing from `0x5b` (91) to `0x57` (87) maps directly to **Daily Quota Remaining** (e.g., 87% remaining = 13% used).
    *   A byte decreasing from `0x0b` (11) to `0x08` (8) maps directly to **Weekly Quota Remaining** (e.g., 8% remaining = 92% used).
*   **Unlimited Tier:** Indicated by `-1` (`\xff`) values in binary for low-tier background services.

### The Chargeable Event
When you interact with the AI (e.g., sending "hi"), a request is made to:
*   **Endpoint:** `POST https://server.self-serve.windsurf.com/exa.api_server_pb.ApiServerService/GetChatMessage`
*   This request includes your `Authorization` token, effectively deducting 1 credit from your quota.

## 4. Telemetry and Mandatory Reporting
Windsurf aggressively tracks how the AI features are used. This tracking is **mandatory** for the IDE to function.

*   **Endpoint:** `POST https://server.self-serve.windsurf.com/exa.product_analytics_pb.ProductAnalyticsService/RecordAnalyticsEvent`
*   **Event:** `CASCADE_STEP_COMPLETED`
*   **Critical Discovery:** If this reporting endpoint is blocked (e.g., via a proxy script), the IDE will receive the AI response but **refuse to display it**, instead showing a **"Model provider unreachable"** error. 
*   **Conclusion:** The IDE requires a "synchronous usage receipt" (successful analytics post) before it finalizes the UI state.

## 5. Feature Flags (Unleash)
Windsurf uses Unleash (`unleash.codeium.com`) to manage feature flags and enforce client-side rules dynamically.
*   **Relevant Flags Detected:**
    *   `CASCADE_ENFORCE_QUOTA: "yes"` (Confirms strict server-side limits).
    *   `SHOW_API_PRICING_CREDITS_USED: "yes"` (Enables UI elements showing credit usage).
    *   `planName: "Trial"` (Passed in query parameters to tailor the experience).

## Summary
To "change" a user or bypass a trial limit, one would need to reset the `installationId`, generate a new `devin-session-token`, and obtain a fresh `Authorization` header. Simple blocking of telemetry (`RecordAnalyticsEvent`) is not viable as it causes the IDE's internal state machine to fail and block AI output.