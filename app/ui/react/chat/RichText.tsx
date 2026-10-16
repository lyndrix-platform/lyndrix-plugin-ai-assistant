import React from 'react'

/**
 * Deliberately small markdown-ish renderer for assistant text: fenced code
 * blocks, `inline code`, **bold**, bullet lists, `#` headings, links and line
 * breaks. Everything is built as React elements — no HTML injection, no
 * markdown dependency in the bundle.
 */

type Block =
  | { type: 'code'; lang: string; code: string }
  | { type: 'list'; items: string[] }
  | { type: 'heading'; text: string }
  | { type: 'para'; lines: string[] }

function splitBlocks(text: string): Block[] {
  const blocks: Block[] = []
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  let i = 0
  let para: string[] = []
  const flushPara = () => {
    if (para.length) blocks.push({ type: 'para', lines: para })
    para = []
  }
  while (i < lines.length) {
    const line = lines[i]
    const fence = /^\s*```\s*([\w+-]*)\s*$/.exec(line)
    if (fence) {
      flushPara()
      const lang = fence[1] ?? ''
      const code: string[] = []
      i++
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { code.push(lines[i]); i++ }
      i++ // closing fence (or EOF)
      blocks.push({ type: 'code', lang, code: code.join('\n') })
      continue
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      flushPara()
      const items: string[] = []
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ''))
        i++
      }
      blocks.push({ type: 'list', items })
      continue
    }
    const heading = /^\s*#{1,6}\s+(.*)$/.exec(line)
    if (heading) {
      flushPara()
      blocks.push({ type: 'heading', text: heading[1] })
      i++
      continue
    }
    if (line.trim() === '') {
      flushPara()
      i++
      continue
    }
    para.push(line)
    i++
  }
  flushPara()
  return blocks
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|https?:\/\/[^\s<>)]+)/g

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const out: React.ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  INLINE.lastIndex = 0
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) out.push(<code key={`${keyPrefix}-${k++}`}>{tok.slice(1, -1)}</code>)
    else if (tok.startsWith('**')) out.push(<strong key={`${keyPrefix}-${k++}`}>{tok.slice(2, -2)}</strong>)
    else out.push(<a key={`${keyPrefix}-${k++}`} href={tok} target="_blank" rel="noreferrer noopener">{tok}</a>)
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function renderLines(lines: string[], keyPrefix: string): React.ReactNode[] {
  const out: React.ReactNode[] = []
  lines.forEach((l, idx) => {
    if (idx > 0) out.push(<br key={`${keyPrefix}-br-${idx}`} />)
    out.push(...renderInline(l, `${keyPrefix}-l${idx}`))
  })
  return out
}

export default function RichText({ text, className }: { text: string; className?: string }) {
  const blocks = React.useMemo(() => splitBlocks(text), [text])
  return (
    <div className={`lxai-rich${className ? ` ${className}` : ''}`}>
      {blocks.map((b, i) => {
        const key = `b${i}`
        if (b.type === 'code') {
          return (
            <pre key={key} className="lx-terminal lxai-pre" style={{ padding: '0.55rem 0.7rem', position: 'relative' }}>
              {b.lang && <span style={{ position: 'absolute', top: 4, right: 8, fontSize: '0.62rem', opacity: 0.6 }}>{b.lang}</span>}
              {b.code}
            </pre>
          )
        }
        if (b.type === 'list') {
          return <ul key={key}>{b.items.map((it, j) => <li key={j}>{renderInline(it, `${key}-${j}`)}</li>)}</ul>
        }
        if (b.type === 'heading') return <h4 key={key}>{renderInline(b.text, key)}</h4>
        return <p key={key}>{renderLines(b.lines, key)}</p>
      })}
    </div>
  )
}
