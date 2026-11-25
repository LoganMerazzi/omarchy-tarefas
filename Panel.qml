import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "logan.tarefas"
  ipcTarget: "logan.tarefas"
  manageIpc: false

  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("tarefas.py").toString().replace(/^file:\/\//, ""))
  readonly property string dataFile: (Quickshell.env("XDG_DATA_HOME") || (Quickshell.env("HOME") + "/.local/share")) + "/tarefas/tarefas.json"

  // Settings
  readonly property int nagIntervalMin: intSetting("nagIntervalMin", 60, 5, 1440)
  readonly property int overdueIntervalMin: intSetting("overdueIntervalMin", 30, 5, 1440)
  readonly property int maxSnoozes: intSetting("maxSnoozes", 3, 0, 99)
  readonly property int snoozeMin: intSetting("snoozeMin", 60, 5, 1440)
  readonly property int quietStart: intSetting("quietStart", 22, 0, 23)
  readonly property int quietEnd: intSetting("quietEnd", 8, 0, 23)

  // State from tarefas.py list
  property var pending: []
  property var done: []
  property int overdueCount: 0
  property string lastError: ""
  property var preview: null
  property var queue: []

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string glyph: overdueCount > 0 ? "󰄱" : (pending.length > 0 ? "󰄱" : "󰄲")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function intSetting(name, fallback, lo, hi) {
    var v = parseInt(String(setting(name, fallback)), 10)
    if (isNaN(v)) v = fallback
    return Math.max(lo, Math.min(hi, v))
  }

  // ── Helper calls ───────────────────────────────────────────────────────

  function run(args) {
    queue = queue.concat([args])
    pump()
  }

  function pump() {
    if (actionProc.running || queue.length === 0) return
    var next = queue[0]
    queue = queue.slice(1)
    actionProc.command = ["python3", "-I", root.helperPath].concat(next)
    actionProc.running = true
  }

  function reload() {
    if (listProc.running) { reloadAgain = true; return }
    listProc.running = true
  }
  property bool reloadAgain: false

  function applyList(raw) {
    var s
    try { s = JSON.parse(String(raw || "").trim()) } catch (e) { lastError = "Falha ao ler as tarefas"; return }
    if (!s || !Array.isArray(s.pending)) { lastError = "Falha ao ler as tarefas"; return }
    pending = s.pending.slice(0, 200)
    done = (s.done || []).slice(0, 6)
    overdueCount = Number(s.overdueCount || 0)
    lastError = ""
  }

  function tick() {
    if (tickProc.running) return
    tickProc.command = ["python3", "-I", root.helperPath, "tick",
      "--interval", String(nagIntervalMin), "--overdue-interval", String(overdueIntervalMin),
      "--max-snoozes", String(maxSnoozes), "--snooze", String(snoozeMin),
      "--quiet-start", String(quietStart), "--quiet-end", String(quietEnd)]
    tickProc.running = true
  }

  function addTask(text) {
    var t = String(text || "").trim()
    if (!t) return
    run(["add", t])
    addField.text = ""
    preview = null
  }

  function requestPreview() {
    var t = addField.text.trim()
    if (!t) { preview = null; return }
    previewTimer.restart()
  }

  // ── Formatting ─────────────────────────────────────────────────────────

  function heroMeta() {
    if (pending.length === 0) return "Tudo em dia"
    var s = pending.length === 1 ? "1 pendente" : pending.length + " pendentes"
    if (overdueCount > 0) s += " · " + (overdueCount === 1 ? "1 atrasada" : overdueCount + " atrasadas")
    return s
  }

  function barText() {
    if (pending.length === 0) return glyph
    return glyph + " " + pending.length
  }

  function priorityText(p) {
    return p === 1 ? "Alta" : (p === 3 ? "Baixa" : "Média")
  }

  function taskMeta(t) {
    var parts = []
    if (t.priority === 1) parts.push("󰈅 Alta")
    else if (t.priority === 3) parts.push("Baixa")
    if (t.dueText) parts.push((t.overdue ? "Atrasada · " : "") + t.dueText)
    if (t.snoozed) {
      var d = new Date(t.snoozeUntil * 1000)
      parts.push("󰒲 até " + d.toLocaleTimeString(Qt.locale("pt_BR"), "HH:mm"))
    }
    var left = root.maxSnoozes - (t.snoozes || 0)
    if (left <= 0) parts.push("sem adiamentos")
    return parts.join("  ·  ")
  }

  function previewText() {
    if (!preview || !preview.title) return ""
    var s = "→ " + preview.title
    if (preview.dueText) s += "  ·  " + preview.dueText
    if (preview.priority !== 2) s += "  ·  " + priorityText(preview.priority)
    return s
  }

  onOpenedChanged: if (opened) {
    reload()
    Qt.callLater(function() { addField.forceActiveFocus() })
  }

  // ── Processes / timers ─────────────────────────────────────────────────

  Process {
    id: listProc
    command: ["python3", "-I", root.helperPath, "list"]
    running: false
    stdout: StdioCollector { id: listOut; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode === 0) root.applyList(listOut.text)
      if (root.reloadAgain) { root.reloadAgain = false; root.reload() }
    }
  }

  Process {
    id: actionProc
    running: false
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: actionErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.lastError = exitCode === 0 ? "" : String(actionErr.text || "Falhou").trim().slice(-160)
      root.reload()
      root.pump()
    }
  }

  Process {
    id: tickProc
    running: false
    stdout: StdioCollector { waitForEnd: true }
    onExited: root.reload()
  }

  Process {
    id: previewProc
    running: false
    stdout: StdioCollector { id: previewOut; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0 || addField.text.trim() === "") { root.preview = null; return }
      try { root.preview = JSON.parse(String(previewOut.text).trim()) } catch (e) { root.preview = null }
    }
  }

  Timer {
    id: previewTimer
    interval: 220
    onTriggered: {
      if (previewProc.running) { restart(); return }
      previewProc.command = ["python3", "-I", root.helperPath, "parse", addField.text.trim()]
      previewProc.running = true
    }
  }

  // Cobrança: confere a cada minuto. O primeiro tick espera um pouco depois
  // do login para não disparar junto com o resto da sessão.
  Timer {
    interval: 60 * 1000
    repeat: true
    running: true
    onTriggered: root.tick()
  }
  Timer {
    interval: 20 * 1000
    running: true
    onTriggered: root.tick()
  }

  // Mudanças vindas do terminal ou dos botões da notificação.
  FileView {
    path: root.dataFile
    watchChanges: true
    printErrors: false
    onFileChanged: root.reload()
  }

  Component.onCompleted: reload()

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.reload(); return "ok" }
    function tick(): string { root.tick(); return "ok" }
  }

  // ── Bar button ─────────────────────────────────────────────────────────

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText()
    active: root.overdueCount > 0
    horizontalMargin: 8.75
    tooltipText: root.opened ? "" : (root.pending.length > 0 ? root.pending[0].title + (root.pending.length > 1 ? "  (+" + (root.pending.length - 1) + ")" : "") : "Tudo em dia")
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.tick()
      else root.toggle()
    }
  }

  // ── Popup ──────────────────────────────────────────────────────────────

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: addField
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: addField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            title: "Tarefas"
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.overdueCount > 0 ? "󰀦" : "󰄲"
                color: root.overdueCount > 0 ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          TextField {
            id: addField
            width: parent.width
            placeholderText: "Nova tarefa…  ex: Pagar boleto amanhã 10h !"
            foreground: root.foreground
            font.family: root.fontFamily
            maximumLength: 200
            onTextChanged: root.requestPreview()
            onAccepted: root.addTask(text)
            Keys.onEscapePressed: function(event) {
              if (text !== "") text = ""
              else root.close()
              event.accepted = true
            }
            Keys.onTabPressed: function(event) { root.switchPanel(1); event.accepted = true }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: text !== ""
            text: addField.text.trim() !== "" ? root.previewText() : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            visible: root.lastError !== ""
            width: parent.width
            text: root.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          PanelSeparator { foreground: root.foreground }

          Text {
            textFormat: Text.PlainText
            visible: root.pending.length === 0
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            topPadding: Style.space(8)
            bottomPadding: Style.space(8)
            text: "Nada pendente. Aproveite. 🎉"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Column {
            id: pendingColumn
            width: parent.width
            spacing: Style.space(2)
            Repeater {
              model: root.pending
              TaskRow {
                required property var modelData
                width: pendingColumn.width
                task: modelData
              }
            }
          }

          PanelSectionHeader {
            visible: root.done.length > 0
            text: "CONCLUÍDAS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Column {
            id: doneColumn
            width: parent.width
            spacing: Style.space(2)
            Repeater {
              model: root.done
              TaskRow {
                required property var modelData
                width: doneColumn.width
                task: modelData
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "! alta · !baixa · hoje · amanhã · sexta · 15/12 · dia 30 · 14h · 9:30 · em 2h"
            color: Qt.darker(root.foreground, 2.2)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  // ── Components ─────────────────────────────────────────────────────────

  component TaskRow: CursorSurface {
    id: row
    property var task: ({})
    readonly property bool isDone: !!task.done
    readonly property bool canSnooze: !isDone && (task.snoozes || 0) < root.maxSnoozes
    readonly property color titleColor: isDone ? root.dim : (task.overdue ? root.urgent : root.foreground)

    hasCursor: mouse.containsMouse
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: mouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.run([row.isDone ? "undo" : "done", row.task.id])
    }

    RowLayout {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: row.isDone ? "󰄵" : (mouse.containsMouse ? "󰄳" : "󰄰")
        color: row.isDone ? root.dim : (row.task.priority === 1 || row.task.overdue ? root.urgent : root.foreground)
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.task.title || ""
          color: row.titleColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.strikeout: row.isDone
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: text !== ""
          text: row.isDone ? "" : root.taskMeta(row.task)
          color: row.task.overdue ? Qt.darker(root.urgent, 1.2) : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        visible: !row.isDone
        iconText: "󰒲"
        tooltipText: row.canSnooze ? "Adiar " + (root.snoozeMin >= 60 ? Math.round(root.snoozeMin / 60) + "h" : root.snoozeMin + " min") : "Sem mais adiamentos"
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: row.canSnooze
        opacity: enabled ? 1 : 0.35
        onClicked: root.run(["snooze", row.task.id, String(root.snoozeMin)])
      }

      PanelActionButton {
        iconText: "󰆴"
        tooltipText: "Apagar"
        foreground: root.foreground
        hoverColor: root.urgent
        fontFamily: root.fontFamily
        onClicked: root.run(["delete", row.task.id])
      }
    }
  }
}
