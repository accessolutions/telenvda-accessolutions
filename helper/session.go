// The peer to peer session itself.
//
// The add-on carries the few messages needed to set the link up, then the two
// computers talk to each other directly whenever the network allows it, falling
// back on the relay servers the add-on handed over otherwise.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"sync"
	"time"

	"github.com/pion/webrtc/v4"
)

// Names of the two channels a session opens. They are created by the publisher,
// which is the side that starts the exchange.
const (
	videoChannel = "video"
	inputChannel = "input"
)

// maxBufferedBytes is the amount of data allowed to pile up before a frame is
// skipped. Skipping keeps the picture current instead of building a growing
// backlog of stale frames on a link that cannot keep up.
const maxBufferedBytes = 1 << 20

// stillFrameInterval is how often a frame is sent even though nothing changed,
// as a cheap safeguard against a viewer left with a blank window.
const stillFrameInterval = 2 * time.Second

type session struct {
	mu         sync.Mutex
	role       string
	allowInput bool
	settings   qualitySettings

	pc       *webrtc.PeerConnection
	video    *webrtc.DataChannel
	input    *webrtc.DataChannel
	pending  []webrtc.ICECandidateInit
	remote   bool
	started  bool
	finished bool

	window   *viewerWindow
	assembly frameAssembler

	quit func()
}

func newSession(quit func()) *session {
	return &session{quit: quit}
}

// start builds the connection. The publisher opens the channels and makes the
// first move, the viewer waits for it.
func (s *session) start(cmd command) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.started {
		return nil
	}
	s.started = true
	s.role = cmd.Role
	s.allowInput = cmd.AllowInput
	s.settings = settingsFor(cmd.Quality, cmd.MaxFps)

	config := webrtc.Configuration{ICEServers: toICEServers(cmd.IceServers)}
	pc, err := webrtc.NewPeerConnection(config)
	if err != nil {
		return err
	}
	s.pc = pc

	pc.OnICECandidate(func(c *webrtc.ICECandidate) {
		if c == nil {
			return
		}
		// The whole description is carried rather than the candidate line alone,
		// so that the other end can add it without having to guess the media it
		// belongs to.
		data, err := json.Marshal(c.ToJSON())
		if err != nil {
			return
		}
		emit(event{Event: "candidate", Candidate: string(data)})
	})

	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		switch state {
		case webrtc.PeerConnectionStateFailed:
			s.fail("connection_failed")
		case webrtc.PeerConnectionStateClosed, webrtc.PeerConnectionStateDisconnected:
			s.finish()
		}
	})

	if s.role == rolePublisher {
		return s.startPublisher()
	}
	pc.OnDataChannel(s.handleIncomingChannel)
	return nil
}

// startPublisher opens the channels and sends the first description.
func (s *session) startPublisher() error {
	ordered := true
	video, err := s.pc.CreateDataChannel(videoChannel, &webrtc.DataChannelInit{Ordered: &ordered})
	if err != nil {
		return err
	}
	s.video = video
	input, err := s.pc.CreateDataChannel(inputChannel, &webrtc.DataChannelInit{Ordered: &ordered})
	if err != nil {
		return err
	}
	s.input = input
	input.OnMessage(func(msg webrtc.DataChannelMessage) {
		s.handleRemoteInput(msg.Data)
	})
	video.OnOpen(func() {
		emit(event{Event: "connected"})
		go s.captureLoop(video)
	})

	offer, err := s.pc.CreateOffer(nil)
	if err != nil {
		return err
	}
	if err := s.pc.SetLocalDescription(offer); err != nil {
		return err
	}
	// The description is sent straight away and the candidates follow as they are
	// discovered, which shortens the wait before the picture appears.
	emit(event{Event: "offer", SDP: offer.SDP})
	return nil
}

// handleIncomingChannel is the viewer side of the two channels the publisher
// opened.
func (s *session) handleIncomingChannel(dc *webrtc.DataChannel) {
	switch dc.Label() {
	case videoChannel:
		dc.OnOpen(func() {
			emit(event{Event: "connected"})
			s.openWindow()
		})
		dc.OnMessage(func(msg webrtc.DataChannelMessage) {
			s.handleFrameChunk(msg.Data)
		})
	case inputChannel:
		s.mu.Lock()
		s.input = dc
		s.mu.Unlock()
	default:
		// A channel we know nothing about has no business being here.
		_ = dc.Close()
	}
}

// handleOffer is answered by the viewer only.
func (s *session) handleOffer(sdp string) error {
	if s.pc == nil || s.role != roleViewer {
		return nil
	}
	if err := s.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdp}); err != nil {
		return err
	}
	s.flushCandidates()
	answer, err := s.pc.CreateAnswer(nil)
	if err != nil {
		return err
	}
	if err := s.pc.SetLocalDescription(answer); err != nil {
		return err
	}
	emit(event{Event: "answer", SDP: answer.SDP})
	return nil
}

// handleAnswer is answered by the publisher only.
func (s *session) handleAnswer(sdp string) error {
	if s.pc == nil || s.role != rolePublisher {
		return nil
	}
	if err := s.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: sdp}); err != nil {
		return err
	}
	s.flushCandidates()
	return nil
}

// handleCandidate adds a route the other end discovered, holding it back until
// its description has arrived since a candidate is meaningless before that.
func (s *session) handleCandidate(raw string) error {
	var candidate webrtc.ICECandidateInit
	if err := json.Unmarshal([]byte(raw), &candidate); err != nil {
		return err
	}
	s.mu.Lock()
	if s.pc == nil {
		s.mu.Unlock()
		return nil
	}
	if !s.remote {
		s.pending = append(s.pending, candidate)
		s.mu.Unlock()
		return nil
	}
	s.mu.Unlock()
	return s.pc.AddICECandidate(candidate)
}

func (s *session) flushCandidates() {
	s.mu.Lock()
	s.remote = true
	pending := s.pending
	s.pending = nil
	s.mu.Unlock()
	for _, candidate := range pending {
		if err := s.pc.AddICECandidate(candidate); err != nil {
			logf("a candidate could not be added: %v", err)
		}
	}
}

// captureLoop sends the shared screen for as long as the session lasts.
func (s *session) captureLoop(dc *webrtc.DataChannel) {
	capturer, err := newCapturer(s.settings.maxWidth)
	if err != nil {
		s.fail("capture_failed")
		return
	}
	defer capturer.close()

	interval := time.Second / time.Duration(s.settings.maxFps)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	var (
		seq      uint16
		previous []byte
		scratch  *image.RGBA
		buf      bytes.Buffer
		lastSent time.Time
	)
	for range ticker.C {
		if s.done() {
			return
		}
		if dc.ReadyState() != webrtc.DataChannelStateOpen {
			return
		}
		// A link that cannot keep up is better served by fewer, fresher frames.
		if dc.BufferedAmount() > maxBufferedBytes {
			continue
		}
		if capturer.sizeChanged() {
			capturer.close()
			capturer, err = newCapturer(s.settings.maxWidth)
			if err != nil {
				s.fail("capture_failed")
				return
			}
			previous = nil
		}
		pixels, err := capturer.grab()
		if err != nil {
			s.fail("capture_failed")
			return
		}
		// A screen reader user leaves the screen still most of the time, so an
		// unchanged picture is simply not sent again.
		if bytes.Equal(pixels, previous) && time.Since(lastSent) < stillFrameInterval {
			continue
		}
		data, newScratch, err := encodeFrame(pixels, capturer.dstW, capturer.dstH, s.settings.jpeg, scratch, &buf)
		if err != nil {
			s.fail("encode_failed")
			return
		}
		scratch = newScratch
		chunks, err := chunkFrame(seq, capturer.dstW, capturer.dstH, data)
		if err != nil {
			continue
		}
		for _, chunk := range chunks {
			if err := dc.Send(chunk); err != nil {
				return
			}
		}
		seq++
		lastSent = time.Now()
		if len(previous) != len(pixels) {
			previous = make([]byte, len(pixels))
		}
		copy(previous, pixels)
	}
}

// handleFrameChunk rebuilds and displays a picture on the viewer.
func (s *session) handleFrameChunk(data []byte) {
	frame, width, height, complete := s.assembly.add(data)
	if !complete {
		return
	}
	pixels, w, h, err := decodeFrame(frame, width, height)
	if err != nil {
		logf("a picture could not be decoded: %v", err)
		return
	}
	s.mu.Lock()
	window := s.window
	s.mu.Unlock()
	if window != nil {
		window.show(pixels, w, h)
	}
}

// handleRemoteInput replays a mouse action on the publisher, if and only if the
// user of that computer agreed to it.
func (s *session) handleRemoteInput(data []byte) {
	if s.role != rolePublisher || !s.allowInput {
		return
	}
	if len(data) > 512 {
		return
	}
	var ev inputEvent
	if err := json.Unmarshal(data, &ev); err != nil {
		return
	}
	replayMouse(ev)
}

// openWindow shows the remote screen. Mouse actions are only reported when the
// controlled computer agreed to be driven, so nothing is sent needlessly.
func (s *session) openWindow() {
	window := newViewerWindow()
	if s.allowInput {
		window.onMouse = s.sendInput
	}
	window.onClose = s.finish
	s.mu.Lock()
	s.window = window
	s.mu.Unlock()
	// Translated titles are not available here, so the window is named after the
	// add-on, which is enough for the user to recognise it.
	go window.run("TeleNVDA - remote screen")
}

func (s *session) sendInput(ev inputEvent) {
	s.mu.Lock()
	input := s.input
	s.mu.Unlock()
	if input == nil || input.ReadyState() != webrtc.DataChannelStateOpen {
		return
	}
	data, err := json.Marshal(ev)
	if err != nil {
		return
	}
	_ = input.SendText(string(data))
}

func (s *session) done() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.finished
}

// fail reports that the session cannot go on and brings it down.
func (s *session) fail(reason string) {
	s.mu.Lock()
	if s.finished {
		s.mu.Unlock()
		return
	}
	s.finished = true
	s.mu.Unlock()
	emitFailure(reason)
	s.shutdown()
}

// finish reports an orderly end of the session.
func (s *session) finish() {
	s.mu.Lock()
	if s.finished {
		s.mu.Unlock()
		return
	}
	s.finished = true
	s.mu.Unlock()
	emit(event{Event: "closed"})
	s.shutdown()
}

// stop ends the session because the add-on asked for it, which needs no event
// since the add-on already knows.
func (s *session) stop() {
	s.mu.Lock()
	s.finished = true
	s.mu.Unlock()
	s.shutdown()
}

func (s *session) shutdown() {
	s.mu.Lock()
	pc := s.pc
	window := s.window
	s.mu.Unlock()
	if window != nil {
		window.close()
	}
	if pc != nil {
		_ = pc.Close()
	}
	if s.quit != nil {
		s.quit()
	}
}

func toICEServers(servers []iceServer) []webrtc.ICEServer {
	out := make([]webrtc.ICEServer, 0, len(servers))
	for _, server := range servers {
		if len(server.Urls) == 0 {
			continue
		}
		entry := webrtc.ICEServer{URLs: server.Urls}
		if server.Username != "" {
			entry.Username = server.Username
			entry.Credential = server.Credential
		}
		out = append(out, entry)
	}
	return out
}

func logf(format string, args ...any) {
	fmt.Fprintf(stderr, format+"\n", args...)
}
