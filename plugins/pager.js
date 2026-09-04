const DEFAULT_PAGER_URL = "https://pager.ofloo.io"

const unwrap = (value) => value?.data ?? value

const sessionIdFromEvent = (event) => {
  const properties = event.properties ?? {}
  return properties.sessionID ?? properties.sessionId ?? properties.session?.id ?? null
}

const messageText = (page) => {
  const lines = ["Pagerbericht ontvangen:"]
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

  const findSession = async () => {
    if (activeSession) return activeSession
    const response = unwrap(await client.session.list())
    const sessions = Array.isArray(response) ? response : response?.sessions ?? []
    const candidate = sessions
      .filter((session) => session.id)
      .sort((left, right) => String(right.time?.updated ?? right.updated ?? "").localeCompare(String(left.time?.updated ?? left.updated ?? "")))[0]
    activeSession = candidate?.id ?? null
    return activeSession
  }

  const deliver = async (page) => {
    const sessionId = await findSession()
    if (!sessionId) return
    await client.session.prompt({
      path: { id: sessionId },
      body: {
        parts: [{ type: "text", text: messageText(page) }],
      },
    })
  }

  const waitForPager = async () => {
    if (running) return
    running = true
    try {
      while (!stopped) {
        const token = await readToken()
        if (!token) {
          await new Promise((resolve) => setTimeout(resolve, 5000))
          continue
        }
        try {
          const response = await fetch(`${pagerUrl}/mailboxes/${encodeURIComponent(token)}/wait`)
          if (!response.ok) throw new Error(`PagerMCP returned HTTP ${response.status}`)
          await deliver(await response.json())
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
      const eventSession = sessionIdFromEvent(event)
      if (eventSession) activeSession = eventSession
      if (event.type === "session.deleted" && eventSession === activeSession) activeSession = null
    },
  }
}
