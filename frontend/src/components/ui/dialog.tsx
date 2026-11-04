import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils/cn';

export function Dialog({ open, onOpenChange, title, description, children, className }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (!open || dialogRef.current == null) return;
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();

    return () => {
      if (dialog.open) dialog.close();
      previousFocus?.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      className={cn('fixed inset-0 m-auto max-h-[calc(100dvh-2rem)] w-full min-w-0 max-w-2xl overflow-y-auto rounded-xl border bg-card p-6 text-card-foreground shadow-lg outline-none backdrop:bg-background/80 backdrop:backdrop-blur-sm', className)}
      onCancel={(event) => {
        event.preventDefault();
        onOpenChange(false);
      }}
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
          onOpenChange(false);
        }
      }}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1.5">
          <h2 id={titleId} className="text-lg font-semibold leading-none tracking-tight">{title}</h2>
          {description && <p id={descriptionId} className="text-sm text-muted-foreground">{description}</p>}
        </div>
        <Button type="button" variant="ghost" size="icon" className="shrink-0" aria-label="关闭弹窗" onClick={() => onOpenChange(false)}>
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="mt-5">{children}</div>
    </dialog>,
    document.body,
  );
}
