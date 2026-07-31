import { useEffect, useRef } from 'react'
import { Renderer, Program, Mesh, Color, Triangle } from 'ogl'

/**
 * Iridescence — animated oil-slick WebGL backdrop.
 *
 * Shader adapted from react-bits (https://reactbits.dev/backgrounds/iridescence,
 * MIT). The GLSL is upstream's; the wrapper adds the things a long-lived
 * settings surface needs and the demo component does not:
 *
 *  - `prefers-reduced-motion` renders ONE frame and never starts the rAF loop
 *  - the loop stops while the tab is hidden (a settings tab left open for hours
 *    would otherwise pin a core forever)
 *  - devicePixelRatio is capped, since this covers a whole panel
 *  - full teardown on unmount, including forcing context loss
 */

const vertexShader = `
attribute vec2 uv;
attribute vec2 position;

varying vec2 vUv;

void main() {
  vUv = uv;
  gl_Position = vec4(position, 0, 1);
}
`

const fragmentShader = `
precision highp float;

uniform float uTime;
uniform vec3 uColor;
uniform vec3 uResolution;
uniform vec2 uMouse;
uniform float uAmplitude;
uniform float uSpeed;

varying vec2 vUv;

void main() {
  float mr = min(uResolution.x, uResolution.y);
  vec2 uv = (vUv.xy * 2.0 - 1.0) * uResolution.xy / mr;

  uv += (uMouse - vec2(0.5)) * uAmplitude;

  float d = -uTime * 0.5 * uSpeed;
  float a = 0.0;
  for (float i = 0.0; i < 8.0; ++i) {
    a += cos(i - d - a * uv.x);
    d += sin(uv.y * i + a);
  }
  d += uTime * 0.5 * uSpeed;
  vec3 col = vec3(cos(uv * vec2(d, a)) * 0.6 + 0.4, cos(a + d) * 0.5 + 0.5);
  col = cos(col * cos(vec3(d, a, 2.5)) * 0.5 + 0.5) * uColor;
  gl_FragColor = vec4(col, 1.0);
}
`

export type IridescenceProps = {
  /** RGB multiplier, 0..1 per channel. Dimmer values suit dark themes. */
  color?: [number, number, number]
  speed?: number
  amplitude?: number
  mouseReact?: boolean
  /** Upper bound on devicePixelRatio; this fills a panel, so 1 is plenty. */
  maxDpr?: number
  className?: string
}

export function Iridescence({
  color = [0.52, 0.86, 0.76],
  speed = 0.8,
  amplitude = 0.1,
  mouseReact = true,
  maxDpr = 1,
  className = '',
}: IridescenceProps) {
  const hostRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    // jsdom (vitest) and WebGL-blocked browsers have no context. Probe first so
    // ogl does not log "unable to create webgl context" on every test render.
    const probe = document.createElement('canvas')
    if (!probe.getContext('webgl') && !probe.getContext('experimental-webgl')) return

    let renderer: Renderer
    try {
      renderer = new Renderer({ alpha: false, powerPreference: 'low-power' })
    } catch {
      return
    }
    const gl = renderer.gl
    if (!gl) return
    gl.clearColor(0, 0, 0, 1)

    const dpr = Math.min(maxDpr, window.devicePixelRatio || 1)
    renderer.dpr = dpr

    const program = new Program(gl, {
      vertex: vertexShader,
      fragment: fragmentShader,
      uniforms: {
        uTime: { value: 0 },
        uColor: { value: new Color(...color) },
        uResolution: { value: new Color(1, 1, 1) },
        uMouse: { value: new Float32Array([0.5, 0.5]) },
        uAmplitude: { value: amplitude },
        uSpeed: { value: speed },
      },
    })
    const mesh = new Mesh(gl, { geometry: new Triangle(gl), program })

    const resize = () => {
      renderer.setSize(host.offsetWidth, host.offsetHeight)
      program.uniforms.uResolution.value = new Color(
        gl.canvas.width,
        gl.canvas.height,
        gl.canvas.width / gl.canvas.height,
      )
    }
    // ResizeObserver, not window.resize: the panel also changes width when the
    // chat sidebar or the side panel nav collapses, which fires no window event.
    const ro = new ResizeObserver(resize)
    ro.observe(host)
    resize()

    const onMouseMove = (e: MouseEvent) => {
      const rect = host.getBoundingClientRect()
      const u = program.uniforms.uMouse.value as Float32Array
      u[0] = (e.clientX - rect.left) / rect.width
      u[1] = 1 - (e.clientY - rect.top) / rect.height
    }
    if (mouseReact) host.addEventListener('mousemove', onMouseMove)

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

    let raf = 0
    const frame = (t: number) => {
      raf = requestAnimationFrame(frame)
      program.uniforms.uTime.value = t * 0.001
      renderer.render({ scene: mesh })
    }
    const start = () => { if (!raf && !reduced) raf = requestAnimationFrame(frame) }
    const stop = () => { if (raf) { cancelAnimationFrame(raf); raf = 0 } }

    if (reduced) {
      // one static frame, at a t that lands on a pleasant part of the loop
      program.uniforms.uTime.value = 2.4
      renderer.render({ scene: mesh })
    } else {
      start()
    }

    const onVisibility = () => (document.hidden ? stop() : start())
    document.addEventListener('visibilitychange', onVisibility)

    // ogl sizes the canvas via width/height attributes only; CSS-size it so it
    // fills the host box regardless of the dpr cap.
    gl.canvas.style.width = '100%'
    gl.canvas.style.height = '100%'
    gl.canvas.style.display = 'block'
    host.appendChild(gl.canvas)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
      ro.disconnect()
      if (mouseReact) host.removeEventListener('mousemove', onMouseMove)
      gl.canvas.parentElement?.removeChild(gl.canvas)
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speed, amplitude, mouseReact, maxDpr, color[0], color[1], color[2]])

  return <div ref={hostRef} aria-hidden className={className} />
}

export default Iridescence
