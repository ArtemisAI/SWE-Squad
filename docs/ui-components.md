# SWE-Squad UI Component Library

## Overview

The SWE-Squad React UI (`ui/src/components/`) contains approximately 55 shared components organized into eight categories. All components are written in TypeScript with explicit prop interfaces, use Tailwind CSS + shadcn/ui primitives, and target the Vite + React Router SPA served from `ui/dist`. The design system uses CSS variables (`bg-background`, `text-foreground`, `border-border`, etc.) so every component respects both light and dark themes automatically.

---

## Categories

### Layout & Navigation

#### Layout

**Purpose.** Root shell that composes the full-page chrome: `TeamRail` + `Sidebar` + main content area + `PropertiesPanel` + `MobileBottomNav`, `CommandPalette`, global `NewTicketDialog`, and a `ToastViewport`.

**Props.** No external props — consumes `ThemeContext`, `SidebarContext`, `DialogContext`, `PanelContext`, and `TeamProvider` internally. Renders child routes via `<Outlet />`.

**Usage example.**
```tsx
// Registered as the root route element in App.tsx
<Route element={<Layout />}>
  <Route path="/" element={<Dashboard />} />
  ...
</Route>
```

**Notes.** Registers global keyboard shortcuts (`Cmd+K` = command palette, `Cmd+I` = new ticket, `Cmd+B` = toggle sidebar, `Cmd+.` = properties panel, `Cmd+T` = toggle theme). On mobile the sidebar closes automatically on route change.

---

#### Sidebar

**Purpose.** 240 px collapsible left navigation listing all app sections, with live badge counts on Inbox, Approvals, and Tickets.

**Props.** None — reads `AuthContext`, `DialogContext`, and `SidebarContext` internally.

**Usage example.**
```tsx
<Sidebar />
```

**Notes.** Polls `/api/inbox/summary`, `/api/approvals`, and `/api/dashboard` every 30 seconds (only when sidebar is open) for badge counts. Renders `SidebarProjects`, `SidebarAgents`, `SidebarBudget`, and `AccountSwitcher` sub-components. Includes a user profile footer with avatar, role badge, and sign-out button. Organized into labeled sections: Main, Work, Fleet, Pipeline, Operations, Settings.

---

#### TeamRail

**Purpose.** Narrow 56 px left edge rail for switching the active team context, with colored avatars and drag-to-reorder.

**Props.**
```ts
interface TeamRailProps {
  className?: string;
}
```

**Usage example.**
```tsx
<TeamRail className="shrink-0" />
```

**Notes.** Reads teams from `TeamContext`. Each team item shows a colored initial avatar (alpha=blue, beta=emerald, gamma=purple, delta=amber). A badge shows the count of live runs for that team. Clicking the "S" org button at the top clears the active team filter (shows all). Supports full keyboard navigation (`Enter`/`Space` to activate). Drag-and-drop reorder calls `useReorderTeams()` from `TeamContext`.

---

#### BreadcrumbBar

**Purpose.** Renders the current page's breadcrumb trail from `BreadcrumbContext` with clickable parent links.

**Props.** None — reads from `BreadcrumbContext`.

**Usage example.**
```tsx
// In a page component, first set breadcrumbs:
useBreadcrumbs([
  { label: "Tickets", href: "/tickets" },
  { label: ticket.title },
]);
// Then render:
<BreadcrumbBar />
```

**Notes.** Returns `null` when there are no breadcrumbs. The context also syncs `document.title`. The last crumb renders as a non-linked `BreadcrumbPage`; all preceding crumbs render as links.

---

#### MobileBottomNav

**Purpose.** Fixed bottom tab bar for mobile viewports, showing Dashboard, Tickets, Create, Agents, and Inbox with live badge counts.

**Props.** None — fetches badge data internally via TanStack Query.

**Usage example.**
```tsx
// Rendered inside Layout automatically
<MobileBottomNav />
```

**Notes.** Shown only via CSS on `sm:hidden` breakpoint. Inbox badge and live agent count are fetched from `/api/inbox` and `/api/heartbeats`.

---

#### CommandPalette

**Purpose.** Global `Cmd+K` search modal for navigating pages, running quick actions, and jumping to tickets, projects, agents, or routines.

**Props.**
```ts
interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}
```

**Usage example.**
```tsx
<CommandPalette open={cmdOpen} onOpenChange={setCmdOpen} />
```

**Notes.** When `open` is `true` and the user types, it fetches matching results in parallel from dashboard tickets, projects, agents, and routines via TanStack Query (`enabled: open && search.length > 0`). When search is empty it shows static Quick Actions (New Ticket `Cmd+I`, New Project `Cmd+P`, New Agent `Cmd+Shift+A`, Toggle Theme `Cmd+T`, Toggle Sidebar `Cmd+B`) and all Navigation pages. Action commands open dialogs via `DialogContext`. Navigate with arrow keys; select with Enter.

---

#### PropertiesPanel

**Purpose.** Collapsible 288 px right sidebar showing editable properties (status, severity, assignee, labels, metadata) for the currently viewed ticket.

**Props.** None — reads URL to extract `ticketId`, controlled via `PanelContext("governor")`.

**Usage example.**
```tsx
// Toggle with Cmd+.
<PropertiesPanel />
```

**Notes.** Automatically detects the ticket ID from the URL pattern `/tickets/:id`. Status and severity changes are saved immediately via `PATCH /api/tickets/:id/status` and `/api/tickets/:id/severity`. When no ticket is in context, shows a help message. Toggle shortcut is `Cmd+.`.

---

### Data Display

#### MetricCard

**Purpose.** Compact stat card displaying a numeric or string value with a label, icon, optional description, and optional navigation link.

**Props.**
```ts
interface MetricCardProps {
  icon: LucideIcon;
  value: string | number;
  label: string;
  description?: ReactNode;
  to?: string;      // router link
  onClick?: () => void;
}
```

**Usage example.**
```tsx
<MetricCard
  icon={CircleDot}
  value={42}
  label="Open Tickets"
  description="3 critical"
  to="/tickets"
/>
```

**Notes.** When `to` is provided, wraps the card in a router `Link`. When only `onClick` is provided, wraps in a `div` with click handler. Hover highlight is applied only when the card is interactive. The `description` is hidden on small screens (`hidden sm:block`).

---

#### StatusBadge

**Purpose.** Inline pill badge that maps a ticket status string to a color-coded style from `lib/status-colors`.

**Props.**
```ts
{ status: string }
```

**Usage example.**
```tsx
<StatusBadge status="in_development" />
```

**Notes.** Converts underscores to spaces for display. Falls back to a neutral default style for unknown status values. Color mappings live in `src/lib/status-colors.ts` (`statusBadge` object).

---

#### ActivityRow

**Purpose.** Single-row list item for the activity feed, showing a ticket's status badge, priority icon, title, source module, and relative timestamp.

**Props.** See `ui/src/components/ActivityRow.tsx` — accepts an `ActivityEvent` or `SWETicket` shaped object.

**Usage example.**
```tsx
<ActivityRow ticket={ticket} />
```

---

#### PriorityIcon

**Purpose.** Colored arrow icon for `CRITICAL | HIGH | MEDIUM | LOW` severity, optionally clickable to open a severity picker popover.

**Props.**
```ts
interface PriorityIconProps {
  severity: TicketSeverity;          // "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
  onChange?: (severity: TicketSeverity) => void;
  className?: string;
  showLabel?: boolean;               // render severity text next to icon
  disabled?: boolean;
}
```

**Usage example.**
```tsx
// Read-only display
<PriorityIcon severity="HIGH" />

// Interactive picker
<PriorityIcon severity={ticket.severity} onChange={handleSeverityChange} showLabel />
```

**Notes.** When `onChange` is provided and `disabled` is false, clicking opens a popover with all four severity options. A `pushToast` confirms the change. Arrow size and color scale with severity (CRITICAL = largest red, LOW = smallest gray).

---

#### Identity

**Purpose.** User/agent avatar with initials, optional name + role text, and an optional online-status badge.

**Props.**
```ts
interface IdentityProps {
  name: string;
  role?: string;
  size?: "xs" | "sm" | "default" | "lg";
  status?: "online" | "offline" | "away" | "busy" | "dnd";
  avatarOnly?: boolean;
  onClick?: () => void;
  className?: string;
}
```

**Usage example.**
```tsx
<Identity name="swe-squad-alpha" role="Senior Agent" status="online" size="sm" />
<Identity name="Alice Smith" avatarOnly size="xs" />
```

**Notes.** Generates initials from the first letters of name words (up to two). Status colors: online=green, offline=gray, away=yellow, busy/dnd=red. `avatarOnly` hides the name+role text. Interactive when `onClick` is provided (cursor pointer, hover opacity).

---

#### CopyText

**Purpose.** Inline button that displays text with a copy icon; clicking it writes the text to the clipboard and shows a brief checkmark confirmation.

**Props.**
```ts
interface CopyTextProps {
  text: string;
  className?: string;
  truncate?: boolean;  // truncates display to 200px max-width
}
```

**Usage example.**
```tsx
<CopyText text={ticket.ticket_id} truncate />
<CopyText text="npm install @dnd-kit/core" />
```

**Notes.** Uses `navigator.clipboard.writeText`. On success, icon changes to a green checkmark for 1.5 s and a tooltip shows "Copied!". On permission error, shows "Permission denied". Cleans up the timeout on unmount.

---

#### GoalCard / GoalProgress

**Purpose.** `GoalCard` renders a project goal summary card; `GoalProgress` renders a progress bar for a goal's completion percentage. Both are defined in `src/components/GoalCard.tsx` and `src/components/GoalProgress.tsx`.

---

### Editors & Input

#### InlineEditor

**Purpose.** Click-to-edit text field that renders as a static element in view mode and switches to an `Input` or `Textarea` in edit mode.

**Props.**
```ts
interface InlineEditorProps {
  value: string;
  onSave: (value: string) => void | Promise<void>;
  onChange?: (value: string) => void;   // immediate notification
  as?: "h1" | "h2" | "p" | "span";     // wrapper element (default: "span")
  placeholder?: string;
  multiline?: boolean;                  // Textarea instead of Input
  mode?: "view" | "edit";              // controlled mode
  className?: string;
  disabled?: boolean;
  showEditIcon?: boolean;              // hover pencil icon (default: true)
  isSaving?: boolean;                  // deprecated alias for disabled
}
```

**Usage example.**
```tsx
<InlineEditor
  value={ticket.title}
  onSave={(v) => updateTicket({ title: v })}
  as="h1"
  placeholder="Untitled ticket"
/>

// Multiline description
<InlineEditor
  value={ticket.description}
  onSave={handleDescriptionSave}
  multiline
  placeholder="Add a description..."
/>
```

**Notes.** Keyboard shortcuts: `Enter` saves (single-line) or `Shift+Enter` inserts newline in multiline mode; `Escape` cancels. Auto-focuses the input and selects all text (single-line) or moves cursor to end (multiline) on entering edit mode. `onBlur` also triggers save. Shows a success toast via `ToastContext` after save. Can be used in controlled mode by passing `mode` prop.

---

#### InlineEntitySelector

**Purpose.** Generic searchable entity picker rendered as a popover combobox, usable for any entity type via TypeScript generics.

**Props.**
```ts
interface InlineEntitySelectorProps<T> {
  value: T | null;
  onChange: (value: T | null) => void;
  entities: T[];
  getLabel: (entity: T) => string;
  getValue: (entity: T) => string;
  renderTrigger?: (selected: T | null, onOpen: () => void) => ReactNode;
  renderOption?: (entity: T, isSelected: boolean) => ReactNode;
  searchable?: boolean;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  className?: string;
  align?: "start" | "center" | "end";
  sideOffset?: number;
  triggerClassName?: string;
  allowUnassigned?: boolean;
  unassignedLabel?: string;
  unassignedValue?: string;
  disabled?: boolean;
}
```

**Usage example.**
```tsx
<InlineEntitySelector
  value={selectedAgent}
  onChange={setSelectedAgent}
  entities={agents}
  getLabel={(a) => a.name}
  getValue={(a) => a.id}
  searchable
  placeholder="Select agent..."
  allowUnassigned
  unassignedLabel="Unassigned"
/>
```

**Notes.** When `searchable` is true, uses `cmdk` `Command` for keyboard-navigable filtering. When false, renders a simple button list. `allowUnassigned` inserts an "Unassigned" option that sets value to `null`. Custom `renderTrigger` and `renderOption` slots allow arbitrary rendering. The default trigger is an outline button with a chevron. Popover width defaults to 224 px (`w-56`).

---

#### MarkdownEditor

**Purpose.** Textarea-based markdown editor with a live-preview toggle, `@mention` autocomplete popover, and Save/Cancel action buttons.

**Props.**
```ts
interface MarkdownEditorProps {
  value?: string;
  onChange?: (value: string) => void;
  onSave?: (value: string) => void;
  onCancel?: () => void;
  isSaving?: boolean;
  placeholder?: string;
  buttonText?: string;              // default "Save"
  showPreview?: boolean;            // controlled preview toggle
  mentionOptions?: MentionOption[]; // [{ id, name, avatar?, role? }]
  onMentionSelect?: (mention: MentionOption) => void;
}
```

**Usage example.**
```tsx
<MarkdownEditor
  onSave={handleAddComment}
  onCancel={() => setEditing(false)}
  isSaving={mutation.isPending}
  placeholder="Write your comment..."
  buttonText="Add Comment"
  mentionOptions={teamMembers}
/>
```

**Notes.** Typing `@` followed by characters opens a mention autocomplete popover. Navigate the popover with `ArrowUp`/`ArrowDown`; select with `Enter`; dismiss with `Escape`. Preview mode renders a `<pre>` monospace preview. The toolbar shows a Preview/Edit toggle button and a hint of supported markdown syntax. Save is disabled when content is empty.

---

#### ScheduleEditor

**Purpose.** Visual cron expression builder with preset options (every 15 min, hourly, daily, weekdays, weekly, monthly) and a custom raw-cron fallback.

**Props.**
```ts
interface ScheduleEditorProps {
  value: string;         // current cron expression (e.g. "0 9 * * 1")
  onChange: (cron: string) => void;
  disabled?: boolean;
  className?: string;
  showValidation?: boolean;  // default true
}
```

**Usage example.**
```tsx
<ScheduleEditor
  value={routine.cron}
  onChange={(cron) => setField("cron", cron)}
/>
```

**Notes.** Parses the initial `value` via `parseCronToPreset()` from `lib/cronUtils`. UI adapts: the time pickers (hour/minute) hide for `every_15_min`; a day-of-week select appears for `weekly`; a day-of-month select for `monthly`; a raw cron input for `custom`. Validation errors from `validateCron()` are shown below the input. Uses shadcn `Select` components internally.

---

#### DateRangePicker

**Purpose.** Dropdown date-range selector with four quick presets (Last 24h, 7d, 30d, Custom) and a custom date input panel.

**Props.**
```ts
type DatePreset = "24h" | "7d" | "30d" | "custom";

interface DateRangePickerProps {
  preset: DatePreset;
  onPresetChange: (preset: DatePreset) => void;
  onCustomRangeChange?: (start: Date, end: Date) => void;
  customStart?: Date;
  customEnd?: Date;
}
```

**Usage example.**
```tsx
const [preset, setPreset] = useState<DatePreset>("7d");
const [customStart, setCustomStart] = useState<Date>();
const [customEnd, setCustomEnd] = useState<Date>();

<DateRangePicker
  preset={preset}
  onPresetChange={setPreset}
  onCustomRangeChange={(s, e) => { setCustomStart(s); setCustomEnd(e); }}
/>
```

**Notes.** Selecting a non-custom preset closes the dropdown immediately and calls `onPresetChange`. Selecting "Custom" keeps the dropdown open to show the date inputs. An "Apply Range" button commits the custom range. The trigger button label reflects the active selection (e.g., "Last 7 days" or "4/1/2026 - 4/8/2026"). Positioned with `absolute right-0 top-full` — ensure the parent has `position: relative`.

---

### Content Rendering

#### MarkdownBody

**Purpose.** Full-featured markdown renderer with GitHub Flavored Markdown, syntax highlighting, mermaid diagram support, and HTML sanitization.

**Props.**
```ts
interface MarkdownBodyProps {
  content: string;
  className?: string;
  truncate?: number;  // character limit; truncated content gets "..." appended
}
```

**Usage example.**
```tsx
<MarkdownBody content={ticket.description} />
<MarkdownBody content={longText} truncate={500} />
```

**Notes.** Uses `react-markdown` + `remark-gfm` + `rehype-sanitize`. Code blocks use `react-syntax-highlighter` with the `vscDarkPlus` theme. Mermaid fenced code blocks (` ```mermaid `) are rendered as SVG diagrams using `mermaid.js` with `securityLevel:'strict'` and additional SVG sanitization (strips `<script>` tags, `on*` attributes, `javascript:` URIs). `@username` mentions inside text are rendered as inline accent badges. External links open in `_blank` with `rel="noopener noreferrer"`.

---

#### CommentThread

**Purpose.** Full comment list + add-comment form for a ticket, using `MarkdownEditor` for new comments and mutating via `ticketsApi`.

**Props.**
```ts
interface CommentThreadProps {
  ticketId: string;
  comments: TicketComment[];  // [{ text, source, timestamp }]
}
```

**Usage example.**
```tsx
<CommentThread ticketId={ticket.ticket_id} comments={ticket.comments ?? []} />
```

**Notes.** "Add a comment" button toggles to show a `MarkdownEditor`. On save, calls `ticketsApi.addComment(ticketId, comment)` and invalidates the dashboard query cache. Each comment has a trash icon that calls `ticketsApi.deleteComment(ticketId, index)`. Comments are displayed as monospace `<pre>` blocks with the author's `Identity` component.

---

#### ActivityTimeline

**Purpose.** Vertical timeline of `ActivityEvent` items, each with a colored icon, actor identity, relative time, and optional detail key-value pairs.

**Props.**
```ts
interface ActivityTimelineProps {
  events: ActivityEvent[];   // [{ id, action, actor?, timestamp, details? }]
  className?: string;
}
```

**Usage example.**
```tsx
<ActivityTimeline events={ticket.activity ?? []} />
```

**Notes.** Wrapped in `React.memo`. Recognized `action` types with icons: `status_change` (RotateCw, blue), `assign` (UserPlus, green), `comment` (MessageSquare, purple), `branch` (GitBranch, orange), `pr` (GitPullRequest, cyan), `pr_merged` (GitPullRequest, green), `closed` (X, red), `reopened` (RotateCw, amber). Unknown actions fall back to a bullet dot. The `details` object keys/values render as monospace key: value rows.

---

#### GoalTree

**Purpose.** Collapsible hierarchical tree view of `GoalNode` items linking to ticket detail pages.

**Props.**
```ts
interface GoalTreeProps {
  nodes: GoalNode[];            // from api/goals — { ticket_id, title, status, severity, children? }
  expanded?: Set<string>;       // controlled expand state
  onToggle?: (id: string) => void;
}
```

**Usage example.**
```tsx
// Uncontrolled (manages its own expand state)
<GoalTree nodes={goalNodes} />

// Controlled
<GoalTree nodes={goalNodes} expanded={expandedSet} onToggle={handleToggle} />
```

**Notes.** Nodes indent by 12 px per level. Each node shows a `StatusBadge`, a linked title (`/tickets/:ticket_id`), and an optional severity chip. Click the chevron to expand/collapse children — event propagation is stopped to prevent navigation. Renders a "No tickets in this goal hierarchy" empty state when `nodes` is empty.

---

### Loading & State

#### PageSkeleton

**Purpose.** Placeholder skeleton layout for loading states, with variants matching the structure of each major page type.

**Props.**
```ts
interface PageSkeletonProps {
  variant?:
    | "list"         // default — header + 7 row skeletons
    | "issues-list"  // filter row + 8 row skeletons
    | "detail"       // breadcrumb + title + content sections
    | "dashboard"    // banner + 4 metric cards + 4 charts + 2 large panels
    | "approvals"    // header + 3 card skeletons
    | "costs"        // filter chips + summary + 2 charts
    | "inbox"        // title + 3 grouped sections
    | "org-chart"    // full-height single skeleton
}
```

**Usage example.**
```tsx
if (isLoading) return <PageSkeleton variant="dashboard" />;
```

**Notes.** All variants use shadcn `Skeleton` components. No props beyond `variant` — just drop it in as a loading fallback.

---

#### EmptyState

**Purpose.** Centered empty-state illustration with an icon, message, and optional action button.

**Props.**
```ts
interface EmptyStateProps {
  icon: LucideIcon;
  message: string;
  action?: string;    // button label
  onAction?: () => void;
}
```

**Usage example.**
```tsx
<EmptyState
  icon={CircleDot}
  message="No open tickets"
  action="Create ticket"
  onAction={() => openDialog({ dialogType: "newTicket" })}
/>
```

**Notes.** The action button is only rendered when both `action` and `onAction` are provided. The icon is rendered at 40 px in a muted background square.

---

#### ErrorBoundary

**Purpose.** React class component error boundary that catches render errors and shows a recovery UI with "Try Again" and "Back to Dashboard" options.

**Props.**
```ts
interface Props {
  children: ReactNode;
  fallbackPath?: string;  // default "/"
}
```

**Usage example.**
```tsx
<ErrorBoundary fallbackPath="/tickets">
  <TicketDetail />
</ErrorBoundary>
```

**Notes.** Logs the error and component info via `console.error`. "Try Again" resets state to re-render children. An expandable `<details>` section shows the full stack trace for debugging. The fallback path link uses React Router `Link`.

---

#### PageTabBar

**Purpose.** Responsive tab bar that renders tab buttons on desktop (`sm:` and up) and collapses to a `Select` dropdown on mobile.

**Props.**
```ts
interface Tab {
  id: string;
  label: string;
  icon?: React.ElementType;
  badge?: number;
}

interface PageTabBarProps {
  tabs: Tab[];
  activeTab: string;
  onTabChange: (tabId: string) => void;
  showIconInDropdown?: boolean;  // default true
}
```

**Usage example.**
```tsx
<PageTabBar
  tabs={[
    { id: "overview", label: "Overview", icon: LayoutDashboard },
    { id: "activity", label: "Activity", icon: Activity, badge: 3 },
  ]}
  activeTab={activeTab}
  onTabChange={setActiveTab}
/>
```

**Notes.** Badge numbers render as small counts inside the tab button. The mobile `Select` shows the icon of the active tab in the trigger.

---

#### FilterBar

**Purpose.** Horizontal row of removable filter chips and an optional search input, used above list views.

**Props.**
```ts
export interface Filter {
  category: string;
  value: string;
  label?: string;   // optional display override for value
}

export interface FilterBarProps {
  filters: Filter[];
  onRemoveFilter: (category: string, value: string) => void;
  onClearAll: () => void;
  searchValue?: string;
  onSearchChange?: (value: string) => void;
}
```

**Usage example.**
```tsx
<FilterBar
  filters={[
    { category: "status", value: "open" },
    { category: "severity", value: "HIGH", label: "High" },
  ]}
  onRemoveFilter={(cat, val) => removeFilter(cat, val)}
  onClearAll={clearAllFilters}
  searchValue={search}
  onSearchChange={setSearch}
/>
```

**Notes.** Each chip shows `category: label/value` with an `X` remove button (accessible `aria-label`). "Clear All" button appears only when there is at least one active filter. The search input is shown only when `onSearchChange` is provided. Search box is 224 px wide with an inline `Search` icon.

---

### Interactive Widgets

#### KanbanBoard

**Purpose.** Generic drag-and-drop kanban board with typed columns and items, powered by `@dnd-kit`.

**Props.**
```ts
export interface KanbanColumn<T> {
  id: string;
  title: string;
  items: T[];
  color?: string;  // "red" | "green" | "blue" | "orange" | etc.
}

export interface KanbanBoardProps<T> {
  columns: KanbanColumn<T>[];
  onMoveItem: (itemId: string, fromColumn: string, toColumn: string, newIndex: number) => void;
  renderItem: (item: T) => ReactNode;
  getItemId: (item: T) => string;
  className?: string;
  disabled?: boolean;
  onAddItem?: (columnId: string) => void;
}
```

**Usage example.**
```tsx
<KanbanBoard
  columns={[
    { id: "open", title: "Open", items: openTickets, color: "blue" },
    { id: "in_dev", title: "In Development", items: devTickets, color: "purple" },
    { id: "resolved", title: "Resolved", items: resolvedTickets, color: "green" },
  ]}
  onMoveItem={handleMove}
  renderItem={(ticket) => <TicketCard ticket={ticket} />}
  getItemId={(ticket) => ticket.ticket_id}
  onAddItem={(colId) => openNewTicketDialog(colId)}
/>
```

**Notes.** Uses `@dnd-kit/core` `DndContext` + `@dnd-kit/sortable` `SortableContext`. Each item has a `GripVertical` drag handle. During drag a `DragOverlay` renders a shadow copy of the dragged card with a ring outline. Pointer sensor requires 8 px of movement before activating (prevents accidental drags). Keyboard sensor supports full arrow-key navigation. Each column is 320 px wide; the board scrolls horizontally. `disabled` grays out the entire board.

---

#### LiveRunWidget

**Purpose.** Real-time agent run status panel for a ticket, showing LIVE/PAUSED status, transcript log lines, and a cancel button, with 3-second auto-polling.

**Props.**
```ts
interface LiveRunWidgetProps {
  ticketId: string;
  variant?: "compact" | "full";  // default "full"
  autoPoll?: boolean;            // default true
}
```

**Usage example.**
```tsx
// Full panel on ticket detail page
<LiveRunWidget ticketId={ticket.ticket_id} />

// Compact inline version
<LiveRunWidget ticketId={ticket.ticket_id} variant="compact" autoPoll={false} />
```

**Notes.** Polls `heartbeatsApi.getActiveRunForIssue(ticketId)` every 3 s. Only fetches transcript (`heartbeatsApi.getTranscript`) while the run is live. Auto-scrolls transcript to bottom; shows "Scroll to bottom" button when user scrolls up. Cancel button calls `heartbeatsApi.cancelRun(ticketId)`. Color-coded transcript levels: info=muted, warning=amber, error=red, debug=dim. Returns `null` in compact mode when there is no active run.

---

#### RepoPickerDialog

**Purpose.** Modal dialog for connecting GitHub repositories to the current account, with search and per-repo connect toggle.

**Props.**
```ts
interface RepoPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  connectedRepos: string[];  // already-connected repo full_names
}
```

**Usage example.**
```tsx
<RepoPickerDialog
  open={repoPickerOpen}
  onOpenChange={setRepoPickerOpen}
  connectedRepos={connectedRepos}
/>
```

**Notes.** Fetches GitHub repos via `githubApi`. Displays star count, branch count, visibility lock, and last-push date. Each row has a "Connect" or "Connected" button. Search filters in-memory by name.

---

#### NewTicketDialog

**Purpose.** Modal form for creating a new `SWETicket` with title, description (markdown), status, severity, labels, assignee, and optional project/goal.

**Props.** None — controlled via `DialogContext`. Open with `openDialog({ dialogType: "newTicket" })`.

**Usage example.**
```tsx
const { openDialog } = useDialog();
<Button onClick={() => openDialog({ dialogType: "newTicket" })}>
  New Ticket
</Button>
// NewTicketDialog is rendered once inside Layout
```

**Notes.** Submits via `ticketsApi.create(...)` and invalidates dashboard/tickets queries on success. Severity picker uses `PriorityIcon` component. Description field uses `MarkdownEditor`.

---

### Charts (`ui/src/components/charts/`)

#### RunActivityChart / PriorityChart / IssueStatusChart / SuccessRateChart

All four charts live in `src/components/charts/ActivityCharts.tsx` and are built on `recharts` (`BarChart`, `LineChart`, `PieChart`) wrapped in `ResponsiveContainer`.

| Export | Chart type | Input type |
|--------|-----------|------------|
| `RunActivityChart` | Stacked bar chart | `RunActivityPoint[]` — `{ date, investigations, fixes_attempted, fixes_succeeded }` |
| `PriorityChart` | Pie chart | `PriorityData[]` — `{ priority, count }` |
| `IssueStatusChart` | Horizontal bar chart | `StatusDataPoint[]` — `{ status, count }` |
| `SuccessRateChart` | Line chart | `SuccessRatePoint[]` — `{ date, rate }` |

Each chart accepts `height?: number`, `showLegend?: boolean`, and `className?: string`. All are wrapped in a `ChartCard` container with a title and optional `headerAction` slot.

**Usage example.**
```tsx
import { RunActivityChart, PriorityChart } from "@/components/charts/ActivityCharts";

<RunActivityChart data={activityData} height={200} showLegend />
<PriorityChart data={priorityBreakdown} />
```

---

#### StatusChart

**Purpose.** Custom SVG horizontal bar chart showing ticket counts by workflow status, with color-coded bars per status.

**Props.**
```ts
interface StatusChartProps {
  data: Record<string, number>;  // { "open": 12, "in_development": 5, ... }
}
```

**Usage example.**
```tsx
<StatusChart data={dashboardData.tickets_by_status} />
```

**Notes.** Pure SVG — no recharts dependency. Renders a labeled horizontal bar for each status with a proportional fill. Color map covers all known statuses (open=blue, resolved=green, failed=red, etc.). Wrapped in `React.memo`.

---

#### SeverityChart

**Purpose.** Custom SVG donut chart showing ticket distribution across severity levels (CRITICAL / HIGH / MEDIUM / LOW).

**Props.**
```ts
interface SeverityChartProps {
  data: Record<string, number>;  // { "critical": 3, "high": 7, ... }
}
```

**Usage example.**
```tsx
<SeverityChart data={dashboardData.tickets_by_severity} />
```

**Notes.** Pure SVG donut using stroke-dasharray arcs. Colors: critical=red, high=orange, medium=yellow, low=gray. Shows a legend list with percentage labels. Wrapped in `React.memo`.

---

#### PRPipelineChart

**Purpose.** Custom SVG funnel chart visualizing the PR lifecycle stages: Created → Reviewed → Merged → Verified.

**Props.**
```ts
interface PRPipelineChartProps {
  metrics: PRLifecycleMetrics;
  // { prs_created_total, prs_reviewed_total, prs_merged_total, verification_pass_total }
}
```

**Usage example.**
```tsx
<PRPipelineChart metrics={dashboardData.pr_lifecycle} />
```

**Notes.** Pure SVG with proportional bar heights and arrow connectors between stages. Wrapped in `React.memo`. Includes `role="img"` and `aria-label` for accessibility.

---

### Transcript & Logs

#### RunTranscriptView

**Purpose.** Detailed transcript viewer for a completed or live `AgentRun`, with "Nice" formatted view, raw monospace view, compact/full density toggle, and auto-scroll.

**Props.**
```ts
interface RunTranscriptViewProps {
  run: AgentRun | null;
  transcript: TranscriptEntry[];   // [{ timestamp, level, message, source? }]
  agentId?: string;
  agentName?: string;
}
```

**Usage example.**
```tsx
<RunTranscriptView
  run={activeRun}
  transcript={transcriptEntries}
  agentName="your-bot-beta"
/>
```

**Notes.** Toggle between "Nice" (colored, tool-call highlighting) and "Raw" (plain `<pre>` ISO timestamps) view modes. "Nice" mode detects `[Tool: ToolName]` patterns and renders them as collapsible inline tool calls with expandable input/output. Transcript level colors: info=muted, warning=amber, error=red, debug=dim. Auto-scroll disables when user manually scrolls up; a "Scroll to bottom" floating button re-enables it. Shows a terminal empty-state when no transcript data exists.

---

#### ScrollToBottom

**Purpose.** Floating "scroll to bottom" button that appears when a scrollable container is scrolled up past a threshold, with optional unread badge.

**Props.**
```ts
export interface ScrollToBottomProps {
  containerRef: React.RefObject<HTMLElement>;
  threshold?: number;               // px from bottom to show button (default 100)
  position?: "bottom-right" | "bottom-left" | "bottom-center";
  icon?: ReactNode;
  badgeCount?: number | null;
  label?: string;
  className?: string;
  variant?: "default" | "outline" | "ghost" | "secondary";
  size?: "default" | "sm" | "lg" | "icon";
  animate?: boolean;                // slide-in animation (default true)
}
```

**Usage example.**
```tsx
const containerRef = useRef<HTMLDivElement>(null);

<div ref={containerRef} className="overflow-y-auto h-96">
  {messages.map(m => <MessageRow key={m.id} message={m} />)}
</div>
<ScrollToBottom containerRef={containerRef} threshold={150} />
```

**Notes.** Also exports:
- `useScrollToBottom(containerRef, threshold)` — hook returning `{ isScrolledUp, isNearBottom, scrollToBottom, checkScrollPosition }`.
- `AutoScrollToBottom` — wrapper component that auto-scrolls children when `enabled` and user is near the bottom.
- `ScrollToBottomWithCount` — variant that only shows when both scrolled up **and** `unreadCount > 0`.
- `useUnreadItems(items, containerRef, itemHeightEstimate)` — hook that tracks new items added to a list while scrolled away from the bottom.

Uses `ResizeObserver` to detect content changes. Respects safe-area insets for mobile (CSS variables `--safe-area-inset-*`). Returns `null` when user is at the bottom.
