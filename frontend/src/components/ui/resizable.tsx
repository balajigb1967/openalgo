import { GripVerticalIcon } from 'lucide-react'
import type * as React from 'react'
import { Group, Panel, Separator } from 'react-resizable-panels'

import { cn } from '@/lib/utils'

function ResizablePanelGroup({ className, ...props }: React.ComponentProps<typeof Group>) {
  return (
    <Group
      data-slot="resizable-panel-group"
      className={cn('flex h-full w-full data-[panel-group-direction=vertical]:flex-col', className)}
      {...props}
    />
  )
}

function ResizablePanel({ ...props }: React.ComponentProps<typeof Panel>) {
  return <Panel data-slot="resizable-panel" {...props} />
}

function ResizableHandle({
  withHandle,
  className,
  orientation,
  ...props
}: React.ComponentProps<typeof Separator> & {
  withHandle?: boolean
  orientation?: 'horizontal' | 'vertical'
}) {
  return (
    <Separator
      data-slot="resizable-handle"
      className={cn(
        'bg-border/80 hover:bg-primary/50 active:bg-primary/80 relative flex items-center justify-center transition-colors focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-ring select-none touch-none',
        orientation === 'horizontal' &&
          'h-1.5 w-full cursor-row-resize after:absolute after:inset-x-0 after:top-1/2 after:h-3 after:-translate-y-1/2',
        orientation === 'vertical' &&
          'w-1.5 h-full cursor-col-resize after:absolute after:inset-y-0 after:left-1/2 after:w-3 after:-translate-x-1/2',
        !orientation && [
          'w-1.5 h-full cursor-col-resize after:absolute after:inset-y-0 after:left-1/2 after:w-3 after:-translate-x-1/2',
          '[&[aria-orientation=horizontal]]:h-1.5 [&[aria-orientation=horizontal]]:w-full [&[aria-orientation=horizontal]]:cursor-row-resize [&[aria-orientation=horizontal]]:after:inset-x-0 [&[aria-orientation=horizontal]]:after:top-1/2 [&[aria-orientation=horizontal]]:after:h-3 [&[aria-orientation=horizontal]]:after:w-full [&[aria-orientation=horizontal]]:after:-translate-y-1/2 [&[aria-orientation=horizontal]]:after:translate-x-0',
          '[&[aria-orientation=vertical]]:w-1.5 [&[aria-orientation=vertical]]:h-full [&[aria-orientation=vertical]]:cursor-col-resize',
          'data-[panel-group-direction=vertical]:h-1.5 data-[panel-group-direction=vertical]:w-full data-[panel-group-direction=vertical]:cursor-row-resize',
        ],
        className
      )}
      {...props}
    >
      {withHandle && (
        <div
          className={cn(
            'bg-background z-10 flex items-center justify-center rounded-xs border border-border/80 shadow-xs transition-colors',
            orientation === 'horizontal'
              ? 'h-2 w-6'
              : orientation === 'vertical'
                ? 'h-6 w-2'
                : 'h-4 w-3 [&[aria-orientation=horizontal]>div]:rotate-90'
          )}
        >
          <GripVerticalIcon
            className={cn('size-2 text-muted-foreground', orientation === 'horizontal' && 'rotate-90')}
          />
        </div>
      )}
    </Separator>
  )
}

export { ResizablePanelGroup, ResizablePanel, ResizableHandle }
