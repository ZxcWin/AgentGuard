import { nextTick, onUnmounted, watch, type Ref } from 'vue'

export function useModal(element: Ref<HTMLElement | null>, open: Ref<boolean>, close: () => void, restoreTarget?: Ref<HTMLElement | null>) {
  let previous: HTMLElement | null = null
  let previousOverflow = ''
  watch(open, async (value, wasOpen) => {
    if (!value && !wasOpen) return
    if (value) {
      previous = restoreTarget?.value || document.activeElement as HTMLElement
      previousOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
      await nextTick()
      element.value?.querySelector<HTMLElement>('button, a, input, select, textarea, [tabindex="0"]')?.focus()
    } else {
      document.body.style.overflow = previousOverflow
      await nextTick()
      // Vue may remove an overlay/backdrop in the same flush. Restore after
      // that flush so the trigger is no longer inert or covered.
      requestAnimationFrame(() => previous?.focus())
    }
  }, { immediate: true })
  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return }
    if (event.key !== 'Tab') return
    const items = element.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a, input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')
    if (!items?.length) return
    const first = items[0]!, last = items[items.length - 1]!
    if (event.shiftKey && (document.activeElement === first || document.activeElement === element.value)) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }
  onUnmounted(() => { if (open.value) document.body.style.overflow = previousOverflow })
  return keydown
}
