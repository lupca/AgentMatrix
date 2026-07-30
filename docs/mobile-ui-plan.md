# Plan: Mobile-First UI Architecture

## Context

Hệ thống hiện tại chỉ có giao diện desktop với sidebar cố định (w-64). Cần kiến trúc lại để:
1. **Mobile-first**: Chat là trung tâm trên mobile (như Google Gemini)
2. **Desktop**: Giữ layout hiện tại nhưng responsive hơn
3. **Drawer thống nhất**: Một drawer trái chứa cả conversations và menu

---

## Kiến trúc đề xuất

### Mobile (< 768px)
```
┌─────────────────────────────────────┐
│  [☰]  Control Tower       [⋯]      │  ← Header với hamburger
├─────────────────────────────────────┤
│                                     │
│         FULL-SCREEN CHAT            │  ← Chat chiếm toàn màn hình
│         (như Gemini)                │
│                                     │
├─────────────────────────────────────┤
│  [💬 message input...        📎 ▶] │
└─────────────────────────────────────┘

Khi mở drawer (vuốt phải hoặc tap ☰):
┌──────────────┬──────────────────────┐
│  DRAWER      │                      │
│ ┌──────────┐ │    (overlay dim)     │
│ │💬 Chats  │ │                      │
│ │📁 Menu   │ │                      │
│ ├──────────┤ │                      │
│ │ Tab content│                      │
│ │ - Session 1│                      │
│ │ - Session 2│                      │
│ │   ...     │                      │
│ └──────────┘ │                      │
└──────────────┴──────────────────────┘
```

### Desktop (≥ 768px)
```
┌────────┬───────────────────────┬────────────┐
│  NAV   │      MAIN CONTENT     │   CHAT     │
│ w-64   │    (pages, etc.)      │  w-[400px] │
│        │                       │  (docked)  │
└────────┴───────────────────────┴────────────┘
```

---

## Components cần tạo/sửa

### 1. New: `src/layouts/ResponsiveLayout.tsx`
- Wrapper component detect mobile/desktop
- Render MobileLayout hoặc DesktopLayout

### 2. New: `src/layouts/MobileLayout.tsx`
- Full-screen chat mặc định
- Header với hamburger button
- MobileDrawer integration

### 3. New: `src/layouts/DesktopLayout.tsx`
- Extract logic từ App.tsx hiện tại
- Sidebar + main content + docked chat

### 4. New: `src/components/mobile/MobileDrawer.tsx`
- Swipeable drawer từ trái
- Tabs: "Chats" | "Menu"
- Chats tab: Danh sách sessions (reuse SessionSidebar logic)
- Menu tab: Navigation items (reuse từ Navigation.tsx)

### 5. New: `src/components/mobile/MobileHeader.tsx`
- Hamburger button mở drawer
- App title/logo
- Optional actions (theme toggle, etc.)

### 6. New: `src/components/mobile/MobileChatView.tsx`
- Full-screen chat optimized cho mobile
- Larger touch targets
- Swipe gestures

### 7. New: `src/hooks/useIsMobile.ts`
- `const isMobile = useIsMobile()` - true khi < 768px
- Dùng matchMedia để reactive

### 8. Update: `src/contexts/GlobalChatContext.tsx`
- Thêm mode: `'fullscreen'` cho mobile
- Track drawer state

### 9. Update: `src/components/Navigation.tsx`
- Export `navGroups` để reuse trong MobileDrawer
- Thêm responsive classes

### 10. Update: `src/App.tsx`
- Dùng ResponsiveLayout thay vì inline layout
- Simplify structure

### 11. Update: Các pages (Tasks, Projects, etc.)
- Responsive grid/table
- Mobile-optimized filters
- Touch-friendly buttons

---

## File Structure sau khi xong

```
src/
├── layouts/
│   ├── ResponsiveLayout.tsx   # NEW - Switch mobile/desktop
│   ├── MobileLayout.tsx       # NEW - Mobile layout
│   └── DesktopLayout.tsx      # NEW - Desktop layout (từ App.tsx)
├── components/
│   ├── mobile/
│   │   ├── MobileDrawer.tsx   # NEW - Swipeable drawer
│   │   ├── MobileHeader.tsx   # NEW - Top header
│   │   └── MobileChatView.tsx # NEW - Full-screen chat
│   ├── Navigation.tsx         # UPDATE - Export nav data
│   └── chat/
│       └── ... (existing)
├── hooks/
│   └── useIsMobile.ts         # NEW
└── App.tsx                    # UPDATE - Use ResponsiveLayout
```

---

## Thứ tự implementation

### Phase 1: Foundation
1. `useIsMobile.ts` hook
2. `ResponsiveLayout.tsx` wrapper
3. `DesktopLayout.tsx` (extract từ App.tsx)
4. Update `App.tsx` dùng ResponsiveLayout

### Phase 2: Mobile Core
5. `MobileHeader.tsx`
6. `MobileLayout.tsx` (basic structure)
7. `MobileChatView.tsx` (full-screen chat)

### Phase 3: Drawer
8. Update `Navigation.tsx` - export nav data
9. `MobileDrawer.tsx` với tabs

### Phase 4: Polish
10. Touch optimizations
11. Page responsive updates (Tasks, Projects)
12. Back navigation từ pages về chat

---

## Verification

1. **Mobile browser**: Mở trên điện thoại hoặc DevTools mobile mode
2. **Test scenarios**:
   - Chat hiển thị full-screen mặc định
   - Swipe/tap hamburger mở drawer
   - Switch giữa Chats/Menu tabs
   - Chọn session → chat update
   - Chọn menu item → navigate đến page
   - Resize window → switch layout smoothly
3. **Desktop**: Đảm bảo layout cũ vẫn hoạt động

---

## Quyết định đã xác nhận

1. **Drawer**: Chỉ hamburger button (không swipe gesture) - đơn giản hơn
2. **Animation**: Minimal - chỉ animation cơ bản, ưu tiên performance
3. **Navigation**: Full-screen pages với nút back quay lại chat
