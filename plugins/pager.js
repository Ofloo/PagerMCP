const DEFAULT_PAGER_URL = "https://pager.ofloo.io"
import { chmodSync } from "node:fs"
// 0 = no server-side age filter: queued pages are still delivered, and old ones
// are marked STALE. Set PAGER_MAX_AGE_MS to a positive value to expire pages
// older than that on the server (they are dropped and never delivered).
const DEFAULT_MAX_AGE_MS = Number(process.env.PAGER_MAX_AGE_MS || 0)
// Server-side long-poll window. It MUST be shorter than the reverse proxy's idle
// timeout (observed at 120s), otherwise every poll dies as a proxy 504 instead of
// a clean 408 and the client logs a warning every cycle.
const WAIT_SECONDS = Number(process.env.PAGER_WAIT_SECONDS || 55)

const unwrap = (value) => value?.data ?? value

const sessionIdFromEvent = (event) => {
  const properties = event.properties ?? {}
  return properties.sessionID ?? properties.sessionId ?? properties.session?.id ?? null
}

const ageSeconds = (page) => {
  if (typeof page?.age_seconds === "number") return Math.max(0, Math.round(page.age_seconds))
  const created = page?.created_at
  const ms = typeof created === "number" ? created * 1000 : typeof created === "string" ? Date.parse(created) : NaN
  return Number.isNaN(ms) ? null : Math.max(0, Math.round((Date.now() - ms) / 1000))
}

const messageText = (page) => {
  const lines = ["Pagerbericht ontvangen:"]
  const age = ageSeconds(page)
  if (age !== null && age >= 900) {
    lines.push(`STALE: this page is ${age} seconds old and may already be superseded; verify before acting.`)
  }
  for (const [key, value] of Object.entries(page)) {
    if (value !== undefined && value !== null) lines.push(`${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
  }
  return lines.join("\n")
}

const PAGER_SYSTEM_INSTRUCTION = `PagerMCP background delivery is active.
When you receive a prompt starting with "Pagerbericht ontvangen:", treat it as an asynchronous external job notification with fields such as PROJECT, JOB_ID, status, message, and logs.
Acknowledge the event concisely, interpret status/logs, continue the related workflow, and report any failure.
Do not manually call wait_for_event or poll the pager; the Pager Plugin listens in the background and delivers notifications automatically.`

export const PagerPlugin = async ({ client, directory }) => {
  const pagerUrl = (process.env.PAGER_URL || DEFAULT_PAGER_URL).replace(/\/$/, "")
  const sessionPath = process.env.PAGER_SESSION_FILE || `${directory}/.pager_session`
  const configuredSession = process.env.PAGER_SESSION_ID || null
  let activeSession = configuredSession
  let knownSessions = new Set()
  // Ids already handed to the session. Delivery is at-least-once: if the server
  // did not see our ack (transient 503, network hiccup) it re-sends the same page.
  // Re-ack it but do not prompt the session twice.
  const deliveredIds = new Set()
  const deliveredOrder = []
  const rememberDelivered = (id) => {
    if (!id || deliveredIds.has(id)) return
    deliveredIds.add(id)
    deliveredOrder.push(id)
    if (deliveredOrder.length > 512) deliveredIds.delete(deliveredOrder.shift())
  }
  let stopped = false
  let running = false

  await client.app.log({
    body: {
      service: "pager-plugin",
      level: "info",
      message: `Pager Plugin loaded for ${directory}`,
    },
  }).catch(() => {})

  const readToken = async () => {
    const file = Bun.file(sessionPath)
    if (!(await file.exists())) return null
    const token = (await file.text()).trim()
    return token || null
  }

  const ensureToken = async () => {
    const existing = await readToken()
    if (existing) return existing
    // Provision the mailbox if no client (docker MCP / pager.sh) has yet created
    // .pager_session, so background delivery does not depend on the MCP tool.
    try {
      const response = await fetch(`${pagerUrl}/mailboxes`, { method: "POST" })
      if (!response.ok) return null
      const created = (await response.json())?.uuid
      if (!created) return null
      await Bun.write(sessionPath, created + "\n")
      try {
        chmodSync(sessionPath, 0o600)
      } catch {}
      return created
    } catch {
      return null
    }
  }

  const normalize = (value) => String(value).replace(/\\/g, "/").replace(/\/+$/, "")
  const inDirectory = (session) => {
    const dir = session?.directory ?? session?.path?.directory
    return typeof dir === "string" && normalize(dir) === normalize(directory)
  }

  const listLocalSessions = async () => {
    const response = unwrap(await client.session.list())
    const sessions = Array.isArray(response) ? response : response?.sessions ?? []
    return sessions.filter((session) => session.id && !session.parentID && inDirectory(session))
  }

  const findSession = async () => {
    if (activeSession && knownSessions.has(activeSession)) return activeSession
    // Only ever target a top-level session in THIS project directory, so a page is
    // never delivered into another project's tab or a sub-agent conversation.
    const local = await listLocalSessions()
    knownSessions = new Set(local.map((session) => session.id))
    const candidate = local.sort((left, right) => String(right.time?.updated ?? right.updated ?? "").localeCompare(String(left.time?.updated ?? left.updated ?? "")))[0]
    activeSession = candidate?.id ?? null
    return activeSession
  }

  const ack = async (token, pageId) => {
    await fetch(`${pagerUrl}/mailboxes/${encodeURIComponent(token)}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: pageId }),
    }).catch(() => {})
  }

  const deliver = async (token, page) => {
    try {
      const sessionId = await findSession()
      if (!sessionId) throw new Error("no active session in this directory; page left queued")
      // promptAsync hands the message to opencode and returns at once. The
      // synchronous prompt would block this loop for the whole agent turn, so the
      // ack (and the queue removal) would wait until the reply finished.
      const session = client.session
      const send = session.promptAsync ? session.promptAsync.bind(session) : session.prompt.bind(session)
      const response = await send({
        path: { id: sessionId },
        body: { parts: [{ type: "text", text: messageText(page) }] },
      })
      const error = unwrap(response)?.info?.error ?? response?.error
      if (error) throw new Error(`session prompt failed: ${JSON.stringify(error)}`)
      // Acknowledge only after a successful hand-off. If this throws, the page
      // stays queued and the next poll retries it instead of losing it.
      await ack(token, page.id)
    } catch (error) {
      await client.app.log({ body: { service: "pager-plugin", level: "warn", message: `Pager delivery failed, page kept queued: ${String(error)}` } }).catch(() => {})
      throw error
    }
  }

  const waitForPager = async () => {
    if (running) return
    running = true
    try {
      while (!stopped) {
        const token = await ensureToken()
        if (!token) {
          await new Promise((resolve) => setTimeout(resolve, 5000))
          continue
        }
        try {
          const maxAgeSeconds = Math.max(0, Math.floor(DEFAULT_MAX_AGE_MS / 1000))
          const params = new URLSearchParams({ timeout_seconds: String(Math.max(1, Math.floor(WAIT_SECONDS))) })
          if (maxAgeSeconds > 0) params.set("max_age_seconds", String(maxAgeSeconds))
          const response = await fetch(`${pagerUrl}/mailboxes/${encodeURIComponent(token)}/wait?${params.toString()}`)
          if (response.status === 408) continue // normal idle timeout: poll again at once
          if (!response.ok) throw new Error(`PagerMCP returned HTTP ${response.status}`)
          const page = await response.json()
          if (page?.id && deliveredIds.has(page.id)) {
            await ack(token, page.id) // duplicate redelivery: drop it, do not re-prompt
            continue
          }
          await deliver(token, page)
          rememberDelivered(page?.id)
        } catch (error) {
          await client.app.log({ body: { service: "pager-plugin", level: "warn", message: String(error) } }).catch(() => {})
          await new Promise((resolve) => setTimeout(resolve, 5000))
        }
      }
    } finally {
      running = false
    }
  }

  waitForPager()

  return {
    "experimental.chat.system.transform": async (_input, output) => {
      output.system.push(PAGER_SYSTEM_INSTRUCTION)
    },
    event: async ({ event }) => {
      const info = event.properties?.info
      if (info?.id && inDirectory(info) && !info.parentID) {
        knownSessions.add(info.id)
        if (event.type !== "session.deleted") activeSession = info.id
      }
      const direct = sessionIdFromEvent(event)
      if (direct && knownSessions.has(direct)) activeSession = direct
      if (event.type === "session.deleted" && (info?.id ?? direct) === activeSession) {
        activeSession = null
        if (info?.id) knownSessions.delete(info.id)
      }
    },
  }
}
