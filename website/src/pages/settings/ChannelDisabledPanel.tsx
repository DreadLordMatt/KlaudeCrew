import { Lock } from 'lucide-react'

/**
 * Shown in place of a channel's editable config panel when the `channels`
 * governance policy DENIES that channel ("Off by admin"). The real panel (with
 * the bot-token form) must NOT render for a denied channel — otherwise a user
 * could type/save config that will never take effect (the backend gates the
 * transport start, see the `channels` chokepoint). Parametrized by channel
 * label so one component serves Discord / Telegram / Webex / WeCom.
 */
export function ChannelDisabledPanel({ label }: { label: string }) {
  return (
    <div className="py-10 flex flex-col items-center text-center max-w-md mx-auto">
      <div className="w-12 h-12 rounded-full bg-bg-hover border border-border flex items-center justify-center mb-4">
        <Lock size={20} className="lucide-inline text-muted" />
      </div>
      <div className="text-base font-semibold text-text-strong mb-1.5">
        {label} is turned off by your administrator
      </div>
      <p className="text-sm text-muted leading-relaxed">
        Your organization's security policy disables this channel. Its settings
        are unavailable and any configuration here would not take effect.
      </p>
    </div>
  )
}
