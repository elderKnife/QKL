package prototype

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	secp "github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
)

func EnsureIdentity(privatePath, publicPath string) (*secp.PrivateKey, error) {
	if err := os.MkdirAll(filepath.Dir(privatePath), 0700); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(publicPath), 0755); err != nil {
		return nil, err
	}
	var key *secp.PrivateKey
	if raw, err := os.ReadFile(privatePath); err == nil {
		b, e := hex.DecodeString(strings.TrimSpace(string(raw)))
		if e != nil {
			return nil, e
		}
		key = secp.PrivKeyFromBytes(b)
	} else if !os.IsNotExist(err) {
		return nil, err
	} else {
		key, err = secp.GeneratePrivateKey()
		if err != nil {
			return nil, err
		}
		if err := os.WriteFile(privatePath, []byte(hex.EncodeToString(key.Serialize())+"\n"), 0600); err != nil {
			return nil, err
		}
	}
	return key, os.WriteFile(publicPath, []byte(hex.EncodeToString(key.PubKey().SerializeCompressed())+"\n"), 0644)
}

func Sign(key *secp.PrivateKey, payload []byte) string {
	h := sha256.Sum256(payload)
	return hex.EncodeToString(ecdsa.Sign(key, h[:]).Serialize())
}

func Verify(pubHex string, payload []byte, signature string) error {
	pubBytes, err := hex.DecodeString(strings.TrimSpace(pubHex))
	if err != nil {
		return fmt.Errorf("invalid trusted public key: %w", err)
	}
	pub, err := secp.ParsePubKey(pubBytes)
	if err != nil {
		return fmt.Errorf("invalid trusted public key: %w", err)
	}
	sigBytes, err := hex.DecodeString(signature)
	if err != nil {
		return fmt.Errorf("invalid signature encoding: %w", err)
	}
	sig, err := ecdsa.ParseDERSignature(sigBytes)
	if err != nil {
		return fmt.Errorf("invalid signature: %w", err)
	}
	h := sha256.Sum256(payload)
	if !sig.Verify(h[:], pub) {
		return fmt.Errorf("signature verification failed")
	}
	return nil
}

func NewPacketID(edgeID string) string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return fmt.Sprintf("%s-%x", edgeID, b[:])
}
