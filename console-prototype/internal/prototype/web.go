package prototype

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type WebConfig struct{ EdgeURL, Edge2URL, VPPURL, GridURL, Token, AssetDir, VPPUsername, VPPPassword, GRIDUsername, GRIDPassword string }

func RunWeb(ctx context.Context) error {
	config := WebConfig{EdgeURL:env("WEB_EDGE_URL", "http://edge:8080"),Edge2URL:env("WEB_EDGE2_URL","http://edge2:8080"),VPPURL:env("WEB_VPP_URL", "http://vpp:8081"),GridURL:env("WEB_GRID_URL","http://grid:8083"),Token:env("INTERNAL_CONTROL_TOKEN", ""),AssetDir:env("WEB_ASSET_DIR", "/app/web"),VPPUsername:env("VPP_USERNAME","vpp"),VPPPassword:env("VPP_PASSWORD","vpp-local-password"),GRIDUsername:env("GRID_USERNAME","grid"),GRIDPassword:env("GRID_PASSWORD","grid-local-password")}
	handler, err := NewWebHandler(config)
	if err != nil {
		return err
	}
	server := &http.Server{Addr: env("WEB_HOST", "0.0.0.0") + ":" + env("WEB_PORT", "8082"), Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 60 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Printf("web listening address=%s mode=local-accounts realBlockchain=false", server.Addr)
	err = server.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func NewWebHandler(config WebConfig) (http.Handler, error) {
	if config.EdgeURL=="" {config.EdgeURL="http://edge:8080"}; if config.Edge2URL=="" {config.Edge2URL="http://edge2:8080"}; if config.VPPURL=="" {config.VPPURL="http://vpp:8081"}; if config.GridURL=="" {config.GridURL="http://grid:8083"}
	if config.VPPUsername=="" {config.VPPUsername="vpp"}; if config.VPPPassword=="" {config.VPPPassword="vpp-local-password"}; if config.GRIDUsername=="" {config.GRIDUsername="grid"}; if config.GRIDPassword=="" {config.GRIDPassword="grid-local-password"}
	if config.Token == "" {
		return nil, fmt.Errorf("INTERNAL_CONTROL_TOKEN is required")
	}
	if config.VPPUsername==""||config.VPPPassword==""||config.GRIDUsername==""||config.GRIDPassword=="" { return nil,fmt.Errorf("VPP and GRID local account credentials are required") }
	if config.VPPUsername==config.GRIDUsername{return nil,fmt.Errorf("VPP and GRID usernames must be different")}
	mux := http.NewServeMux()
	sessions:=newSessionStore(config)
	sessions.routes(mux)
	targets := map[string]*url.URL{}
	for name, raw := range map[string]string{"edge": config.EdgeURL,"edge2":config.Edge2URL, "vpp": config.VPPURL,"grid":config.GridURL} {
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Path != "" {
			return nil, fmt.Errorf("invalid %s upstream", name)
		}
		targets[name] = u
	}
	for name, target := range targets {
		name, target := name, target
		proxy := httputil.NewSingleHostReverseProxy(target)
		original := proxy.Director
		proxy.Director = func(r *http.Request) {
			original(r)
			r.URL.Path = "/v1/ui/" + strings.TrimPrefix(r.URL.Path, "/api/"+name+"/")
			r.URL.RawPath = ""
			r.Host = target.Host
			r.Header.Set("Authorization", "Bearer "+config.Token)
			r.Header.Del("Cookie")
			r.Header.Del("Origin")
		}
		proxy.Transport = &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 3 * time.Second}).DialContext, ResponseHeaderTimeout: 12 * time.Second, IdleConnTimeout: 60 * time.Second}
		proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
			apiError(w, 503, "UPSTREAM_UNAVAILABLE", name, "服务连接失败，数据或操作结果尚未确认", true)
		}
		routes:=[]string{"GET devices", "GET overview", "GET batches", "GET batches/{packetId}", "GET batches/{packetId}/raw", "GET batches/{packetId}/records", "GET records", "GET events", "GET runs", "GET batches/{packetId}/verifications", "POST simulation/start", "POST simulation/pause", "POST simulation/sample-once", "POST batches/{packetId}/retry", "POST batches/{packetId}/verify"}
		if name=="grid" { routes=[]string{"GET overview","GET batches","GET records","GET batches/{packetId}","GET batches/{packetId}/records","GET batches/{packetId}/verifications","POST batches/{packetId}/verify"} }
		if name=="edge2" { routes=[]string{"GET devices", "GET overview", "GET batches", "GET batches/{packetId}", "GET batches/{packetId}/raw", "GET batches/{packetId}/records", "GET records", "GET events", "GET runs", "POST simulation/start", "POST simulation/pause", "POST simulation/sample-once", "POST batches/{packetId}/retry"} }
		if name=="vpp" { routes=append(routes,"GET workbench/overview") }
		for _, route := range routes {
			method, path, _ := strings.Cut(route, " ")
			// Expose only the capabilities actually owned by each service.
			if (name == "edge" || name=="edge2") && (strings.HasSuffix(path, "/verify") || strings.HasSuffix(path, "/verifications")) {
				continue
			}
			if name == "vpp" && (path == "devices" || path == "runs" || strings.HasPrefix(path, "simulation/") || strings.HasSuffix(path, "/retry")) {
				continue
			}
			roles:=[]string{"vpp"};if name=="grid" {roles=[]string{"grid"}}
			mux.Handle(method+" /api/"+name+"/"+path, requireRole(sessions,roles,proxy))
		}
	}
	client := &http.Client{Timeout: 4 * time.Second, CheckRedirect: func(r *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}
	mux.HandleFunc("GET /api/overview", func(w http.ResponseWriter, r *http.Request) {
		type dependency struct {
			Available bool   `json:"available"`
			CheckedAt string `json:"checkedAt"`
			Data      any    `json:"data,omitempty"`
			Error     string `json:"error,omitempty"`
		}
		result := map[string]any{"mode": "local-accounts", "realBlockchain": false}
		var mu sync.Mutex
		var wg sync.WaitGroup
		for name, target := range targets {
			wg.Add(1)
			go func(name string, target *url.URL) {
				defer wg.Done()
				d := dependency{CheckedAt: nowUTC()}
				req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, target.String()+"/v1/ui/overview", nil)
				if err == nil {
					req.Header.Set("Authorization", "Bearer "+config.Token)
					var response *http.Response
					response, err = client.Do(req)
					if err == nil {
						defer response.Body.Close()
						if response.StatusCode == 200 {
							err = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&d.Data)
							d.Available = err == nil
						} else {
							err = fmt.Errorf("dependency HTTP %d", response.StatusCode)
						}
					}
				}
				if err != nil {
					d.Error = "服务或数据库不可用，当前统计未能刷新"
				}
				mu.Lock()
				result[name] = d
				mu.Unlock()
			}(name, target)
		}
		wg.Wait()
		result["checkedAt"] = nowUTC()
		writeJSON(w, 200, result)
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"status": "ok", "service": "prototype-web"})
	})
	files := http.FileServer(http.Dir(config.AssetDir))
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			apiError(w, 404, "NOT_FOUND", "route", "接口不存在", false)
			return
		}
		if r.URL.Path == "/" {
			if _, err := os.Stat(filepath.Join(config.AssetDir, "index.html")); err != nil {
				apiError(w, 503, "ASSETS_UNAVAILABLE", "web", "前端资源未构建", true)
				return
			}
			w.Header().Set("Cache-Control", "no-cache")
		}
		files.ServeHTTP(w, r)
	})
	return localWebGuard(protectedLocalAPI(sessions,mux)), nil
}

func localWebGuard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "same-origin")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'")
		host := r.Host
		if h, _, err := net.SplitHostPort(host); err == nil {
			host = h
		}
		if host != "127.0.0.1" && host != "localhost" && host != "::1" {
			apiError(w, 403, "HOST_NOT_ALLOWED", "access", "此操作台仅允许本机访问", false)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			w.Header().Set("Cache-Control", "no-store")
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			origin := r.Header.Get("Origin")
			scheme := "http"
			if r.TLS != nil {
				scheme = "https"
			}
			if origin != "" && origin != scheme+"://"+r.Host {
				apiError(w, 403, "ORIGIN_NOT_ALLOWED", "access", "拒绝跨站操作请求", false)
				return
			}
			if r.Header.Get("Sec-Fetch-Site") == "cross-site" || r.Header.Get("X-Prototype-Request") != "1" {
				apiError(w, 403, "REQUEST_NOT_ALLOWED", "access", "缺少同源操作请求标记", false)
				return
			}
			media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || media != "application/json" {
				apiError(w, 415, "INVALID_CONTENT_TYPE", "request", "控制请求必须使用 JSON", false)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, 1024)
		}
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}
