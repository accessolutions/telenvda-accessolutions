// Capture of the shared screen.
//
// The picture is scaled down while it is copied, so that the cost of shrinking
// it is paid by the graphics interface rather than by the processor, and so that
// only the reduced picture ever has to be converted and compressed.
package main

import (
	"errors"
	"syscall"
	"unsafe"
)

// capturer owns the drawing resources of a capture session. They are created
// once and reused for every frame, since allocating them anew fifteen times a
// second would be both slow and hard on the graphics interface.
type capturer struct {
	screenDC syscall.Handle
	memDC    syscall.Handle
	bitmap   syscall.Handle
	oldObj   uintptr

	srcX, srcY int
	srcW, srcH int
	dstW, dstH int

	info   bitmapInfo
	pixels []byte // The captured picture, four bytes per pixel in blue green red order.
}

// newCapturer prepares a capture of the whole desktop, reduced so that its
// larger side does not exceed maxWidth.
func newCapturer(maxWidth int) (*capturer, error) {
	x, y, w, h := virtualScreen()
	if w <= 0 || h <= 0 {
		return nil, errors.New("the size of the desktop could not be determined")
	}
	dstW, dstH := w, h
	if maxWidth > 0 && w > maxWidth {
		dstW = maxWidth
		dstH = h * maxWidth / w
	}
	// Odd sizes are avoided because they make the compressed picture larger for
	// no visible benefit.
	dstW &^= 1
	dstH &^= 1
	if dstW < 2 || dstH < 2 {
		return nil, errors.New("the desktop is too small to be shared")
	}

	c := &capturer{srcX: x, srcY: y, srcW: w, srcH: h, dstW: dstW, dstH: dstH}
	screenDC, _, _ := procGetDC.Call(0)
	if screenDC == 0 {
		return nil, errors.New("the screen could not be opened")
	}
	c.screenDC = syscall.Handle(screenDC)
	memDC, _, _ := procCreateCompatibleDC.Call(screenDC)
	if memDC == 0 {
		c.close()
		return nil, errors.New("no drawing surface could be created")
	}
	c.memDC = syscall.Handle(memDC)
	bitmap, _, _ := procCreateCompatibleBitmap.Call(screenDC, uintptr(dstW), uintptr(dstH))
	if bitmap == 0 {
		c.close()
		return nil, errors.New("no picture could be created")
	}
	c.bitmap = syscall.Handle(bitmap)
	c.oldObj, _, _ = procSelectObject.Call(memDC, bitmap)
	// Halftone gives a far more readable text than the default mode once the
	// picture is reduced, which matters more here than raw speed.
	procSetStretchBltMode.Call(memDC, halftone)
	procSetBrushOrgEx.Call(memDC, 0, 0, 0)

	c.info = newBitmapInfo(dstW, dstH)
	c.pixels = make([]byte, dstW*dstH*4)
	return c, nil
}

// sizeChanged reports whether the desktop was resized since the capture started,
// in which case the caller has to build a new capturer.
func (c *capturer) sizeChanged() bool {
	x, y, w, h := virtualScreen()
	return x != c.srcX || y != c.srcY || w != c.srcW || h != c.srcH
}

// grab copies the current state of the desktop. The returned slice belongs to
// the capturer and stays valid until the next call.
func (c *capturer) grab() ([]byte, error) {
	ok, _, _ := procStretchBlt.Call(
		uintptr(c.memDC), 0, 0, uintptr(c.dstW), uintptr(c.dstH),
		uintptr(c.screenDC), uintptr(c.srcX), uintptr(c.srcY), uintptr(c.srcW), uintptr(c.srcH),
		srcCopy|captureBlt,
	)
	if ok == 0 {
		return nil, errors.New("the screen could not be copied")
	}
	// The picture has to be unselected before its bits can be read.
	procSelectObject.Call(uintptr(c.memDC), c.oldObj)
	lines, _, _ := procGetDIBits.Call(
		uintptr(c.memDC), uintptr(c.bitmap), 0, uintptr(c.dstH),
		uintptr(unsafe.Pointer(&c.pixels[0])), uintptr(unsafe.Pointer(&c.info)), dibRGBColors,
	)
	procSelectObject.Call(uintptr(c.memDC), uintptr(c.bitmap))
	if int(lines) != c.dstH {
		return nil, errors.New("the captured picture could not be read")
	}
	return c.pixels, nil
}

func (c *capturer) close() {
	if c.bitmap != 0 {
		if c.oldObj != 0 {
			procSelectObject.Call(uintptr(c.memDC), c.oldObj)
		}
		procDeleteObject.Call(uintptr(c.bitmap))
		c.bitmap = 0
	}
	if c.memDC != 0 {
		procDeleteDC.Call(uintptr(c.memDC))
		c.memDC = 0
	}
	if c.screenDC != 0 {
		procReleaseDC.Call(0, uintptr(c.screenDC))
		c.screenDC = 0
	}
}

// replayMouse carries out a mouse action asked for by the viewer.
//
// Only the mouse is ever replayed: the add-on presents this as mouse control and
// nothing else, so no other kind of input is accepted here.
func replayMouse(ev inputEvent) {
	// A position outside the picture cannot come from a viewer showing it, and
	// letting it through would move the pointer to an arbitrary place.
	if ev.X < 0 || ev.X > 1 || ev.Y < 0 || ev.Y > 1 {
		return
	}
	// Absolute coordinates are expressed over a fixed grid spanning the whole
	// desktop, whatever its real size, so nothing has to be scaled here.
	base := mouseInput{
		Dx:    int32(ev.X * 65535),
		Dy:    int32(ev.Y * 65535),
		Flags: mouseAbsolute | mouseVirtual,
	}

	switch ev.Type {
	case "move":
		base.Flags |= mouseMove
	case "down", "up":
		base.Flags |= mouseMove
		down := ev.Type == "down"
		switch ev.Btn {
		case "left":
			base.Flags |= pick(down, uint32(mouseLDown), uint32(mouseLUp))
		case "right":
			base.Flags |= pick(down, uint32(mouseRDown), uint32(mouseRUp))
		case "middle":
			base.Flags |= pick(down, uint32(mouseMDown), uint32(mouseMUp))
		default:
			return
		}
	case "wheel":
		base.Flags |= mouseMove | mouseWheel
		base.MouseData = uint32(int32(ev.Delta))
	default:
		return
	}
	sendMouseInput(base)
}

func pick(cond bool, whenTrue, whenFalse uint32) uint32 {
	if cond {
		return whenTrue
	}
	return whenFalse
}
