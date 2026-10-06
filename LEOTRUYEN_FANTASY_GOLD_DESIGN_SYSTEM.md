# Leo Truyện — Fantasy Gold Design System

> **Mục đích:** Quy chuẩn màu giao diện cho Leo Truyện. Codex phải đọc và tuân thủ file này khi thực hiện các thay đổi UI trong tương lai.
>
> **QUY TẮC QUAN TRỌNG:** Chỉ thay đổi màu accent/brand theo palette Fantasy Gold bên dưới. **KHÔNG được tự ý đổi màu chữ/text hiện tại của ứng dụng.**

## 1. Quy tắc ưu tiên

1. Giữ nguyên màu chữ hiện tại của project.
2. Không tự ý thay đổi `text-primary`, `text-secondary`, `text-muted` hoặc các màu chữ tương đương.
3. Không đổi màu chữ chỉ vì đổi theme sang Fantasy Gold.
4. Fantasy Gold chỉ được dùng để thay thế/điều chỉnh màu accent, brand, active state, progress, button và các thành phần được quy định bên dưới.
5. Không tự tạo thêm một palette màu vàng khác nếu màu hiện tại đã có trong file này.
6. Không đổi background/card/border hiện tại chỉ vì muốn áp dụng Fantasy Gold, trừ khi task UI cụ thể yêu cầu và người dùng cho phép.
7. Nếu một thành phần đang dùng màu chữ hiện tại và chỉ cần đổi accent, chỉ đổi accent — giữ nguyên text color.

---

# 2. Fantasy Gold Palette

## Primary Gold

```css
--fantasy-gold-primary: #D6A84F;
```

**HEX:** `#D6A84F`

Dùng cho:
- Primary brand
- Logo accent
- Primary button
- Active tab
- Active navigation
- Progress bar
- Selected state
- Important interactive elements
- Icon accent
- Focus ring khi phù hợp

Đây là màu Fantasy Gold chính của Leo Truyện.

## Gold Hover

```css
--fantasy-gold-hover: #E3B963;
```

**HEX:** `#E3B963`

Dùng cho:
- Hover button
- Hover interactive element
- Hover state của icon/link có accent gold
- Highlight nhẹ

Không dùng làm màu chữ mặc định.

## Dark Gold

```css
--fantasy-gold-dark: #B98A35;
```

**HEX:** `#B98A35`

Dùng cho:
- Active/pressed state
- Selected state đậm hơn khi cần
- Border/accent phụ
- Shadow/accent tối
- Các trạng thái cần độ tương phản cao hơn với Primary Gold

---

# 3. Base Dark Palette

Palette nền Fantasy Gold tham chiếu:

```css
--fantasy-background: #191A1E;
--fantasy-card: #24262B;
--fantasy-border: #34363C;
```

- Background: `#191A1E`
- Card: `#24262B`
- Border: `#34363C`

> Đây là màu tham chiếu. Nếu project hiện tại đã có background/card/border đang hoạt động tốt, **không tự ý thay đổi chúng** chỉ để áp dụng Fantasy Gold.

---

# 4. Text Colors — DO NOT CHANGE

Các màu chữ dưới đây chỉ là palette tham chiếu:

```css
--fantasy-text-primary: #F3F0E8;
--fantasy-text-secondary: #A9A7A0;
```

**Codex KHÔNG được tự ý đổi màu chữ hiện tại của Leo Truyện sang hai màu trên.**

Nếu project đang có màu chữ riêng:
- Giữ nguyên màu chữ hiện tại.
- Không replace toàn bộ text color.
- Không đổi `text-*` chỉ vì Fantasy Gold.
- Không đổi màu paragraph, heading, metadata, author, timestamp, description, navigation text...
- Không đổi màu placeholder nếu task không yêu cầu.
- Không đổi màu chữ button nếu màu chữ hiện tại đang đúng.

Fantasy Gold là **accent system**, không phải lý do để thay đổi typography color system.

---

# 5. Recommended CSS Variables

Nếu project có hệ thống CSS variables/theme tokens:

```css
:root {
    --fantasy-gold-primary: #D6A84F;
    --fantasy-gold-hover: #E3B963;
    --fantasy-gold-dark: #B98A35;

    --fantasy-background: #191A1E;
    --fantasy-card: #24262B;
    --fantasy-border: #34363C;
}
```

Không bắt buộc phải tạo lại biến text.

Nếu project đã có biến màu tương ứng, **ưu tiên reuse biến hiện tại** và chỉ map accent sang Fantasy Gold.

---

# 6. Usage Rules

## Primary Button

```css
background: #D6A84F;
```

Hover:

```css
background: #E3B963;
```

Pressed/active:

```css
background: #B98A35;
```

**Không được tự ý đổi màu chữ button.** Giữ nguyên màu chữ button hiện tại của project.

## Active Tab

Có thể dùng:

```css
color: #D6A84F;
```

hoặc:

```css
border-color: #D6A84F;
```

Không đổi màu text của các tab không active nếu không cần.

## Progress Bar

```css
background: #D6A84F;
```

Track/background giữ nguyên hệ thống hiện tại.

## Logo / Brand

```css
color: #D6A84F;
```

Không đổi màu chữ/logo còn lại nếu logo hiện tại có nhiều thành phần.

## Icons

Highlight:

```css
color: #D6A84F;
```

Hover:

```css
color: #E3B963;
```

Không biến toàn bộ icon của website thành gold.

## Links

Chỉ các link có vai trò interactive/brand mới nên dùng Fantasy Gold.

Default:

```css
color: #D6A84F;
```

Hover:

```css
color: #E3B963;
```

Nếu link hiện tại đã có màu chữ phù hợp và task không yêu cầu đổi, giữ nguyên.

## Focus State

Có thể dùng:

```css
outline-color: #D6A84F;
```

hoặc:

```css
box-shadow: 0 0 0 2px rgba(214, 168, 79, 0.35);
```

Không làm focus ring quá sáng hoặc quá dày.

---

# 7. Gold Usage Philosophy

Fantasy Gold phải được dùng **tiết chế**.

Mục tiêu:
- Dark UI là nền chính.
- Gold là điểm nhấn.
- Không biến toàn bộ UI thành màu vàng.
- Không tô vàng tất cả button.
- Không tô vàng tất cả text.
- Không tô vàng toàn bộ icon.
- Không dùng gradient vàng nếu không cần.
- Không dùng quá nhiều hiệu ứng glow.

Tinh thần giao diện:

**Dark Fantasy + Antique Gold + Clean UI**

Gold nên giúp người dùng nhận biết:
- Điều gì đang active
- Điều gì có thể click
- Điều gì quan trọng
- Progress hiện tại
- Brand Leo Truyện

---

# 8. Color Hierarchy

### Level 1 — Primary Accent

```text
#D6A84F
```

Dùng nhiều nhất trong các accent.

### Level 2 — Hover / Highlight

```text
#E3B963
```

Chỉ dùng khi tương tác.

### Level 3 — Dark / Pressed

```text
#B98A35
```

Dùng cho active/pressed hoặc accent tối.

### Neutral

```text
#191A1E
#24262B
#34363C
```

Dùng cho nền/card/border khi cần.

### Text

**Giữ nguyên màu chữ hiện tại của project.**

---

# 9. Do Not

Codex **KHÔNG ĐƯỢC**:
- Đổi toàn bộ website sang vàng.
- Đổi toàn bộ màu chữ sang vàng.
- Đổi màu text hiện tại của project mà không được yêu cầu.
- Dùng `#FFD700` thay cho Fantasy Gold.
- Tự tạo thêm nhiều shades vàng không cần thiết.
- Dùng gradient gold ở mọi nơi.
- Thêm glow vàng mạnh vào mọi component.
- Đổi background chỉ vì áp dụng Fantasy Gold nếu không cần.
- Đổi dark/light theme hiện tại nếu task không yêu cầu.
- Rewrite toàn bộ design system chỉ để đổi accent.
- Thay đổi UI/UX không liên quan đến task.
- Chỉnh các component không liên quan.

---

# 10. Existing UI Preservation

Khi sửa một component hiện tại:

Nếu component đang có Background, Text color, Border, Shadow, Spacing và Typography, và task chỉ yêu cầu đổi accent:

**Chỉ đổi accent.**

Ví dụ:

### BEFORE

```css
.button {
    background: var(--old-primary);
    color: var(--existing-button-text);
}
```

### AFTER

```css
.button {
    background: #D6A84F;
    color: var(--existing-button-text);
}
```

Không được biến thành:

```css
.button {
    background: #D6A84F;
    color: #F3F0E8;
}
```

trừ khi người dùng yêu cầu đổi màu chữ.

---

# 11. Tailwind Mapping

Nếu project dùng Tailwind, có thể map:

```js
colors: {
    fantasyGold: {
        DEFAULT: '#D6A84F',
        hover: '#E3B963',
        dark: '#B98A35',
    },
}
```

Ví dụ:

```html
<button class="bg-fantasyGold hover:bg-fantasyGold-hover">
    ...
</button>
```

Nhưng **không tự thay đổi class màu chữ hiện tại**.

---

# 12. Component Guidelines

## Header

Có thể dùng Gold cho:
- Logo accent
- Active navigation
- Selected state
- Important action

Không đổi toàn bộ header thành gold.

## Sidebar

Gold cho:
- Active menu item
- Selected icon
- Indicator

Các item bình thường giữ nguyên màu hiện tại.

## Library

Gold cho:
- Active tab
- Search/filter focus
- Progress
- Primary action
- Selected card state

## Book Card

Gold cho:
- Progress
- Active/selected state
- Primary CTA
- Favorite/important action nếu phù hợp

Không thêm gold vào mọi metadata.

## Reader

Gold cho:
- Progress
- Active chapter
- Reader controls khi active
- TTS active state
- Bookmark active state
- Important actions

Không đổi màu nội dung truyện.

## Ebook

Gold cho:
- Upload primary action
- Active ebook state
- Download/export action
- Translation progress
- Offline indicator khi cần
- Selected state

---

# 13. Accessibility

Không chỉ dựa vào màu sắc để biểu thị trạng thái.

Active state nên có thêm một hoặc nhiều yếu tố:
- border
- underline
- icon
- background
- aria state

Không dùng gold quá nhạt cho text nhỏ.

Không đổi text color chỉ để cố đạt contrast nếu việc đó phá vỡ hệ thống màu chữ hiện tại; nếu phát hiện vấn đề accessibility, báo cáo rõ thay vì tự ý đổi toàn bộ text system.

---

# 14. Animation

Fantasy Gold có thể dùng cho:
- hover transition
- progress animation
- active indicator
- focus transition

Ưu tiên animation nhẹ:

```css
transition: color 150ms ease,
            background-color 150ms ease,
            border-color 150ms ease,
            box-shadow 150ms ease;
```

Không tạo:
- excessive glow
- flashing gold
- animation liên tục không cần thiết

---

# 15. Shadow / Glow

Nếu cần glow, dùng rất nhẹ:

```css
box-shadow: 0 0 0 1px rgba(214, 168, 79, 0.15);
```

hoặc:

```css
box-shadow: 0 4px 18px rgba(214, 168, 79, 0.10);
```

Không dùng glow mạnh cho toàn bộ UI.

---

# 16. Priority Rule for Codex

Khi có xung đột giữa yêu cầu task và file này:

1. Yêu cầu trực tiếp mới nhất của người dùng có ưu tiên cao hơn.
2. Nếu người dùng không nói rõ về màu chữ → **giữ nguyên màu chữ hiện tại**.
3. Nếu người dùng yêu cầu đổi accent → dùng Fantasy Gold.
4. Nếu người dùng yêu cầu đổi text color → chỉ đổi text color được yêu cầu.
5. Nếu không chắc một màu có nên đổi hay không → **không đổi** và giữ nguyên màu hiện tại.

---

# 17. Codex Instruction

Khi bắt đầu bất kỳ UI task nào cho Leo Truyện:

```text
Read and follow the project's Fantasy Gold design system file.

Use Fantasy Gold as the primary accent:
- Primary: #D6A84F
- Hover: #E3B963
- Dark/Pressed: #B98A35

Do NOT change existing text colors unless the user explicitly asks for text color changes.

Do not redesign unrelated components.
Do not replace the entire existing color system.
Reuse existing backgrounds, cards, borders, typography, spacing and component structure unless the task explicitly requires changes.

Apply Fantasy Gold selectively to brand/accent/active/progress/interactive states.

After implementation, audit the diff and verify that no unrelated text colors were changed.
```

---

# 18. Quick Reference

```text
FANTASY GOLD

Primary    #D6A84F
Hover      #E3B963
Dark       #B98A35

Background #191A1E
Card       #24262B
Border     #34363C

TEXT COLORS
DO NOT CHANGE EXISTING PROJECT TEXT COLORS
```

---

# 19. Final Rule

**Fantasy Gold là accent system của Leo Truyện.**

**Không phải text-color replacement system.**

Mọi thay đổi UI trong tương lai phải ưu tiên:

> **Giữ nguyên giao diện hiện có → thay accent cần thiết → không đổi màu chữ → không overdesign.**
