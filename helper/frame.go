// Turning captured pixels into messages and back.
//
// The picture travels as compressed still frames over a data channel rather than
// as a video stream. A video codec would have to be brought in from outside the
// standard library, would need to be built for each platform, and would give
// little in return on the mostly still pictures a screen reader user shares.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"image"
	"image/draw"
	"image/jpeg"
)

// frameHeaderSize is the length of the header prefixed to every chunk.
const frameHeaderSize = 10

// chunkSize keeps each message well below the largest one a data channel is
// guaranteed to carry.
const chunkSize = 16 * 1024

// maxFrameSize bounds what a peer may make us reassemble.
const maxFrameSize = 8 * 1024 * 1024

// qualitySettings gathers everything the quality name stands for.
type qualitySettings struct {
	maxWidth int
	jpeg     int
	maxFps   int
}

func settingsFor(quality string, maxFps int) qualitySettings {
	s := qualitySettings{maxWidth: 1280, jpeg: 55, maxFps: 15}
	switch quality {
	case "low":
		s = qualitySettings{maxWidth: 960, jpeg: 40, maxFps: 8}
	case "high":
		s = qualitySettings{maxWidth: 1920, jpeg: 75, maxFps: 20}
	}
	if maxFps > 0 && maxFps < s.maxFps {
		s.maxFps = maxFps
	}
	if s.maxFps < 1 {
		s.maxFps = 1
	}
	return s
}

// encodeFrame compresses a captured picture. The pixels come in the blue green
// red order the graphics interface produces, so the components are reordered
// while they are copied into the picture handed to the compressor.
func encodeFrame(pixels []byte, width, height, quality int, scratch *image.RGBA, buf *bytes.Buffer) ([]byte, *image.RGBA, error) {
	if len(pixels) < width*height*4 {
		return nil, scratch, errors.New("the captured picture is shorter than its declared size")
	}
	if scratch == nil || scratch.Rect.Dx() != width || scratch.Rect.Dy() != height {
		scratch = image.NewRGBA(image.Rect(0, 0, width, height))
	}
	dst := scratch.Pix
	for i := 0; i < width*height*4; i += 4 {
		dst[i] = pixels[i+2]
		dst[i+1] = pixels[i+1]
		dst[i+2] = pixels[i]
		dst[i+3] = 0xFF
	}
	buf.Reset()
	if err := jpeg.Encode(buf, scratch, &jpeg.Options{Quality: quality}); err != nil {
		return nil, scratch, err
	}
	return buf.Bytes(), scratch, nil
}

// chunkFrame splits a compressed picture into the messages carrying it.
func chunkFrame(seq uint16, width, height int, data []byte) ([][]byte, error) {
	count := (len(data) + chunkSize - 1) / chunkSize
	if count == 0 || count > 0xFFFF {
		return nil, errors.New("the picture cannot be split into messages")
	}
	chunks := make([][]byte, 0, count)
	for i := 0; i < count; i++ {
		end := (i + 1) * chunkSize
		if end > len(data) {
			end = len(data)
		}
		part := data[i*chunkSize : end]
		msg := make([]byte, frameHeaderSize+len(part))
		binary.LittleEndian.PutUint16(msg[0:], seq)
		binary.LittleEndian.PutUint16(msg[2:], uint16(i))
		binary.LittleEndian.PutUint16(msg[4:], uint16(count))
		binary.LittleEndian.PutUint16(msg[6:], uint16(width))
		binary.LittleEndian.PutUint16(msg[8:], uint16(height))
		copy(msg[frameHeaderSize:], part)
		chunks = append(chunks, msg)
	}
	return chunks, nil
}

// frameAssembler rebuilds the pictures a publisher sends.
//
// The data channel delivers messages in order and without loss, so a chunk that
// does not follow the previous one means the sender or the link misbehaved, and
// the whole picture is dropped rather than shown half wrong.
type frameAssembler struct {
	seq      uint16
	expected uint16
	count    uint16
	width    int
	height   int
	buf      []byte
}

// add takes one message and returns a complete picture when the last chunk of a
// frame has arrived.
func (a *frameAssembler) add(msg []byte) ([]byte, int, int, bool) {
	if len(msg) < frameHeaderSize {
		return nil, 0, 0, false
	}
	seq := binary.LittleEndian.Uint16(msg[0:])
	index := binary.LittleEndian.Uint16(msg[2:])
	count := binary.LittleEndian.Uint16(msg[4:])
	width := int(binary.LittleEndian.Uint16(msg[6:]))
	height := int(binary.LittleEndian.Uint16(msg[8:]))
	if count == 0 || width <= 0 || height <= 0 {
		return nil, 0, 0, false
	}
	if index == 0 {
		a.seq = seq
		a.expected = 0
		a.count = count
		a.width = width
		a.height = height
		a.buf = a.buf[:0]
	} else if seq != a.seq || index != a.expected || count != a.count {
		a.count = 0
		return nil, 0, 0, false
	}
	if len(a.buf)+len(msg)-frameHeaderSize > maxFrameSize {
		a.count = 0
		return nil, 0, 0, false
	}
	a.buf = append(a.buf, msg[frameHeaderSize:]...)
	a.expected = index + 1
	if a.expected != a.count {
		return nil, 0, 0, false
	}
	a.count = 0
	return a.buf, a.width, a.height, true
}

// decodeFrame turns a compressed picture into the blue green red bytes the
// display expects.
func decodeFrame(data []byte, expectedW, expectedH int) ([]byte, int, int, error) {
	img, err := jpeg.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, 0, 0, err
	}
	bounds := img.Bounds()
	width, height := bounds.Dx(), bounds.Dy()
	if width <= 0 || height <= 0 || width != expectedW || height != expectedH {
		return nil, 0, 0, errors.New("the received picture does not have the announced size")
	}
	rgba, ok := img.(*image.RGBA)
	if !ok {
		rgba = image.NewRGBA(image.Rect(0, 0, width, height))
		draw.Draw(rgba, rgba.Bounds(), img, bounds.Min, draw.Src)
	}
	pix := rgba.Pix
	for i := 0; i+3 < len(pix); i += 4 {
		pix[i], pix[i+2] = pix[i+2], pix[i]
		pix[i+3] = 0xFF
	}
	return pix, width, height, nil
}
