// Each result belongs to the dialog that was actually shown. Concurrent
// operations queue their prompts instead of losing an unresolved Promise.
export function createConfirmationQueue(onChange) {
  let sequence = 0;
  const pending = [];
  return {
    ask(options) {
      return new Promise(resolve => {
        const dialog = { ...options, id: ++sequence, resolve };
        pending.push(dialog);
        if (pending.length === 1) onChange(dialog);
      });
    },
    resolve(answer, id) {
      if (!pending.length || pending[0].id !== id) return;
      const dialog = pending.shift();
      onChange(pending[0] || null);
      dialog.resolve(answer);
    },
    cancelAll() {
      for (const dialog of pending.splice(0)) dialog.resolve(dialog.choiceMode ? "cancel" : false);
    },
  };
}
