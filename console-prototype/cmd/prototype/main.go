package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"fisco-bcos-lab-handoff/internal/prototype"
)

func main() {
	if len(os.Args) != 2 {
		log.Fatal("usage: prototype identity-init|edge|vpp|grid|web")
	}
	var err error
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch os.Args[1] {
	case "identity-init":
		err = prototype.RunIdentity()
	case "edge":
		err = prototype.RunEdge(ctx)
	case "vpp":
		err = prototype.RunVPP(ctx)
	case "grid":
		err = prototype.RunGrid(ctx)
	case "web":
		err = prototype.RunWeb(ctx)
	default:
		log.Fatal("unknown mode: ", os.Args[1])
	}
	if err != nil {
		log.Fatal(err)
	}
}
