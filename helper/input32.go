//go:build 386 || arm

package main

// mouseInput is the INPUT structure restricted to its mouse variant.
//
// On thirty two bit builds the union that follows the kind of input needs no
// alignment beyond the four bytes the kind itself takes, so the fields follow
// one another without a gap.
type mouseInput struct {
	Type      uint32
	Dx        int32
	Dy        int32
	MouseData uint32
	Flags     uint32
	Time      uint32
	ExtraInfo uintptr
}
