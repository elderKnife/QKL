package prototype

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"
)

type localUser struct { Username, Password, Role string }
type authSession struct { Username, Role string; Expires time.Time }
type sessionStore struct { mu sync.Mutex; users map[string]localUser; sessions map[string]authSession }

func newSessionStore(c WebConfig)*sessionStore{return &sessionStore{users:map[string]localUser{c.VPPUsername:{c.VPPUsername,c.VPPPassword,"vpp"},c.GRIDUsername:{c.GRIDUsername,c.GRIDPassword,"grid"}},sessions:map[string]authSession{}}}

func (s *sessionStore) user(r *http.Request)(authSession,bool){cookie,err:=r.Cookie("prototype_session");if err!=nil{return authSession{},false};s.mu.Lock();defer s.mu.Unlock();session,ok:=s.sessions[cookie.Value];if !ok{return authSession{},false};if time.Now().After(session.Expires){delete(s.sessions,cookie.Value);return authSession{},false};return session,true}

func (s *sessionStore) routes(mux *http.ServeMux){
	mux.HandleFunc("POST /api/auth/login",func(w http.ResponseWriter,r *http.Request){r.Body=http.MaxBytesReader(w,r.Body,4096);var input struct{Username string `json:"username"`;Password string `json:"password"`};if json.NewDecoder(r.Body).Decode(&input)!=nil{apiError(w,400,"INVALID_CREDENTIALS","auth","用户名或密码错误",false);return};user,ok:=s.users[input.Username];if !ok||subtle.ConstantTimeCompare([]byte(user.Password),[]byte(input.Password))!=1{apiError(w,401,"INVALID_CREDENTIALS","auth","用户名或密码错误",false);return};token:=make([]byte,32);if _,err:=rand.Read(token);err!=nil{apiError(w,500,"SESSION_ERROR","auth","无法创建登录会话",true);return};key:=hex.EncodeToString(token);s.mu.Lock();s.sessions[key]=authSession{user.Username,user.Role,time.Now().Add(8*time.Hour)};s.mu.Unlock();http.SetCookie(w,&http.Cookie{Name:"prototype_session",Value:key,Path:"/",HttpOnly:true,SameSite:http.SameSiteStrictMode,MaxAge:28800});writeJSON(w,200,map[string]any{"username":user.Username,"role":user.Role,"expiresIn":28800})})
	mux.HandleFunc("GET /api/auth/me",func(w http.ResponseWriter,r *http.Request){if user,ok:=s.user(r);ok{writeJSON(w,200,map[string]any{"username":user.Username,"role":user.Role});return};apiError(w,401,"UNAUTHENTICATED","auth","请先登录",false)})
	mux.HandleFunc("POST /api/auth/logout",func(w http.ResponseWriter,r *http.Request){if cookie,err:=r.Cookie("prototype_session");err==nil{s.mu.Lock();delete(s.sessions,cookie.Value);s.mu.Unlock()};http.SetCookie(w,&http.Cookie{Name:"prototype_session",Value:"",Path:"/",HttpOnly:true,SameSite:http.SameSiteStrictMode,MaxAge:-1});writeJSON(w,200,map[string]any{"loggedOut":true})})
}

func requireRole(s *sessionStore,roles []string,next http.Handler)http.Handler{return http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){user,ok:=s.user(r);if !ok{apiError(w,401,"UNAUTHENTICATED","auth","请先登录",false);return};for _,role:=range roles{if user.Role==role{next.ServeHTTP(w,r);return}};apiError(w,403,"FORBIDDEN","auth","当前账号无权访问此工作台",false)})}

func protectedLocalAPI(s *sessionStore,next http.Handler)http.Handler{return http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){if !strings.HasPrefix(r.URL.Path,"/api/")||strings.HasPrefix(r.URL.Path,"/api/auth/"){next.ServeHTTP(w,r);return};user,ok:=s.user(r);if !ok{apiError(w,401,"UNAUTHENTICATED","auth","请先登录",false);return};if r.URL.Path=="/api/overview"{next.ServeHTTP(w,r);return};role:="vpp";if strings.HasPrefix(r.URL.Path,"/api/grid/"){role="grid"};if user.Role!=role{apiError(w,403,"FORBIDDEN","auth","当前账号无权访问此工作台",false);return};next.ServeHTTP(w,r)})}
