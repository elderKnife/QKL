package prototype

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type ChainLocator struct {
	BatchID     string `json:"batchId"`
	TxHash      string `json:"txHash"`
	BlockNumber int64  `json:"blockNumber"`
	MerkleRoot  string `json:"merkleRoot"`
	Adapter     string `json:"adapter"`
}

type GridEnvelope struct {
	VPPID   string      `json:"vppId"`
	Batch   SignedBatch `json:"batch"`
	Locator ChainLocator `json:"locator"`
}

func InitGridSchema(db *sql.DB) error {
	for _, statement := range []string{
		`CREATE TABLE IF NOT EXISTS grid_batches (packet_id VARCHAR(191) PRIMARY KEY,vpp_id VARCHAR(64) NOT NULL,edge_id VARCHAR(64) NOT NULL,batch_id VARCHAR(191) NOT NULL,tx_hash CHAR(64) NOT NULL,block_number BIGINT NOT NULL,merkle_root CHAR(64) NOT NULL,record_count INT NOT NULL,payload_hash CHAR(64) NOT NULL,payload_json LONGTEXT NOT NULL,signature VARCHAR(160) NOT NULL,received_at VARCHAR(40) NOT NULL,INDEX grid_batch_source_idx(vpp_id,received_at),INDEX grid_batch_edge_idx(edge_id,received_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS grid_records (record_id VARCHAR(191) PRIMARY KEY,packet_id VARCHAR(191) NOT NULL,device_id VARCHAR(64) NOT NULL,device_type VARCHAR(64) NOT NULL,collected_at VARCHAR(40) NOT NULL,payload_json LONGTEXT NOT NULL,INDEX grid_record_packet_idx(packet_id,record_id),INDEX grid_record_device_idx(device_id,collected_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS grid_verification_runs (id VARCHAR(191) PRIMARY KEY,packet_id VARCHAR(191) NOT NULL,checked_at VARCHAR(40) NOT NULL,status VARCHAR(16) NOT NULL,result_json LONGTEXT NOT NULL,INDEX grid_verify_packet_idx(packet_id,checked_at,id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS grid_events (id BIGINT AUTO_INCREMENT PRIMARY KEY,packet_id VARCHAR(191) NOT NULL,event_type VARCHAR(64) NOT NULL,occurred_at VARCHAR(40) NOT NULL,details_json LONGTEXT NOT NULL,INDEX grid_event_packet_idx(packet_id,id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
	} {
		if _, err := db.Exec(statement); err != nil { return err }
	}
	return nil
}

func RunGrid(ctx context.Context) error {
	db, err := OpenDB(mysqlDSN(env("GRID_SCHEMA", "grid_data")))
	if err != nil { return err }
	defer db.Close()
	if err = InitGridSchema(db); err != nil { return err }
	keyDir := env("GRID_TRUSTED_KEY_DIR", "/public_keys")
	chainURL := env("GRID_CHAIN_QUERY_URL", "http://vpp:8081/v1/grid/chain")
	token := env("INTERNAL_CONTROL_TOKEN", "")
	if token == "" { return fmt.Errorf("INTERNAL_CONTROL_TOKEN is required") }
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { databaseHealth(w,r,db,"grid-full-node",map[string]any{"realBlockchain":false,"chainAdapter":"mysql-mock"}) })
	mux.Handle("POST /v1/batches", bearerAuth(token,http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
		r.Body=http.MaxBytesReader(w,r.Body,8<<20)
		var envelope GridEnvelope
		decoder:=json.NewDecoder(r.Body); decoder.DisallowUnknownFields()
		if err:=decoder.Decode(&envelope); err!=nil { writeJSON(w,400,map[string]any{"error":"invalid batch envelope"}); return }
		p:=envelope.Batch.Payload
		if envelope.VPPID=="" || envelope.Locator.BatchID=="" || envelope.Locator.TxHash=="" || envelope.Locator.BlockNumber<=0 || ValidatePayload(p)!=nil || envelope.Locator.MerkleRoot=="" { writeJSON(w,422,map[string]any{"error":"invalid batch or chain locator"}); return }
		pub,err:=os.ReadFile(filepath.Join(keyDir,p.EdgeID+".pub")); if err!=nil { writeJSON(w,401,map[string]any{"error":"untrusted edge identity"}); return }
		canonical,err:=Canonical(p); if err!=nil || Verify(string(pub),canonical,envelope.Batch.Signature)!=nil { writeJSON(w,401,map[string]any{"error":"invalid edge signature"}); return }
		root,err:=MerkleRoot(p.Records); if err!=nil || root!=envelope.Locator.MerkleRoot { writeJSON(w,422,map[string]any{"error":"batch digest does not match locator"}); return }
		payloadHash:=sha256.Sum256(canonical)
		replay,err:=storeGridBatch(r.Context(),db,envelope,hex.EncodeToString(payloadHash[:])); if err!=nil { if strings.Contains(err.Error(),"different content") { writeJSON(w,409,map[string]any{"error":err.Error()}) } else { writeJSON(w,503,map[string]any{"error":"grid persistence unavailable"}) }; return }
		status:=201; if replay { status=200 } else if _,verifyErr:=verifyGridBatch(r.Context(),db,p.PacketID,keyDir,chainURL,token);verifyErr!=nil{log.Printf("GRID initial verification deferred packetId=%s error=%v",p.PacketID,verifyErr)}
		writeJSON(w,status,map[string]any{"packetId":p.PacketID,"stored":true,"recordCount":p.RecordCount,"vppId":envelope.VPPID})
	})))
	admin:=http.NewServeMux()
	admin.HandleFunc("GET /v1/ui/overview",func(w http.ResponseWriter,r *http.Request){
		var batches,records,passed,failed int64
		if err:=db.QueryRowContext(r.Context(),`SELECT COUNT(*),COALESCE(SUM(record_count),0) FROM grid_batches`).Scan(&batches,&records); err!=nil { uiFailure(w,err); return }
		_ = db.QueryRowContext(r.Context(),`SELECT COALESCE(SUM(status='PASS'),0),COALESCE(SUM(status IN ('FAIL','ERROR')),0) FROM (SELECT packet_id,status,ROW_NUMBER() OVER(PARTITION BY packet_id ORDER BY checked_at DESC,id DESC) rn FROM grid_verification_runs) x WHERE rn=1`).Scan(&passed,&failed)
		writeJSON(w,200,map[string]any{"database":"ok","batchCount":batches,"recordCount":records,"passedBatches":passed,"failedBatches":failed,"chainAdapter":"mysql-mock","realBlockchain":false,"serverTime":nowUTC()})
	})
	admin.HandleFunc("GET /v1/ui/batches",func(w http.ResponseWriter,r *http.Request){
		q:=r.URL.Query(); clauses,args,err:=gridFilters(q,"g");if err!=nil{uiFailure(w,err);return};where:="";if len(clauses)>0{where=" WHERE "+strings.Join(clauses," AND ")}
		selectSQL:=`SELECT g.packet_id AS packetId,g.vpp_id AS vppId,g.edge_id AS edgeId,g.batch_id AS batchId,g.tx_hash AS txHash,g.block_number AS blockNumber,g.merkle_root AS merkleRoot,g.merkle_root AS localMerkleRoot,JSON_UNQUOTE(JSON_EXTRACT((SELECT result_json FROM grid_verification_runs v WHERE v.packet_id=g.packet_id ORDER BY checked_at DESC,id DESC LIMIT 1),'$.chainRoot')) AS chainMerkleRoot,g.record_count AS recordCount,g.received_at AS receivedAt,COALESCE((SELECT status FROM grid_verification_runs v WHERE v.packet_id=g.packet_id ORDER BY checked_at DESC,id DESC LIMIT 1),'NOT_CHECKED') AS verificationStatus`
		if deviceID:=strings.TrimSpace(q.Get("deviceId"));deviceID!=""{predicate:="EXISTS(SELECT 1 FROM grid_records r WHERE r.packet_id=g.packet_id AND r.device_id=?)";if where==""{where=" WHERE "+predicate}else{where+=" AND "+predicate};args=append(args,deviceID)}
		if status:=strings.TrimSpace(q.Get("status"));status!=""{predicate:="COALESCE((SELECT status FROM grid_verification_runs v WHERE v.packet_id=g.packet_id ORDER BY checked_at DESC,id DESC LIMIT 1),'NOT_CHECKED')=?";if where==""{where=" WHERE "+predicate}else{where+=" AND "+predicate};args=append(args,status)}
		page,err:=queryPage(r.Context(),db,q,selectSQL+` FROM grid_batches g`+where+` ORDER BY g.received_at DESC,g.packet_id DESC`,`SELECT COUNT(*) FROM grid_batches g`+where,args...)
		if err!=nil { uiFailure(w,err); return }; writeJSON(w,200,page)
	})
	admin.HandleFunc("GET /v1/ui/records",func(w http.ResponseWriter,r *http.Request){gridRecordsPage(w,r,db,"")})
	admin.HandleFunc("GET /v1/ui/batches/{packetId}/records",func(w http.ResponseWriter,r *http.Request){gridRecordsPage(w,r,db,r.PathValue("packetId"))})
	admin.HandleFunc("GET /v1/ui/batches/{packetId}",func(w http.ResponseWriter,r *http.Request){
		var raw,sig string
		if err:=db.QueryRowContext(r.Context(),`SELECT payload_json,signature FROM grid_batches WHERE packet_id=?`,r.PathValue("packetId")).Scan(&raw,&sig); err!=nil { uiFailure(w,err); return }
		var stored struct{Payload Payload `json:"payload"`;Locator ChainLocator `json:"locator"`;VPPID string `json:"vppId"`}
		if err:=json.Unmarshal([]byte(raw),&stored); err!=nil { uiFailure(w,err); return }
		var root string;var recordCount int
		if err:=db.QueryRowContext(r.Context(),`SELECT merkle_root,record_count FROM grid_batches WHERE packet_id=?`,r.PathValue("packetId")).Scan(&root,&recordCount);err!=nil{uiFailure(w,err);return}
		var status,latest string
		_ = db.QueryRowContext(r.Context(),`SELECT status,result_json FROM grid_verification_runs WHERE packet_id=? ORDER BY checked_at DESC,id DESC LIMIT 1`,r.PathValue("packetId")).Scan(&status,&latest)
		var verification map[string]any;_ = json.Unmarshal([]byte(latest),&verification);chainRoot,_:=verification["chainRoot"].(string)
		result:=map[string]any{"packetId":stored.Payload.PacketID,"vppId":stored.VPPID,"edgeId":stored.Payload.EdgeID,"recordCount":recordCount,"localMerkleRoot":root,"merkleRoot":root,"chainMerkleRoot":chainRoot,"txHash":stored.Locator.TxHash,"blockNumber":stored.Locator.BlockNumber,"batchId":stored.Locator.BatchID,"verificationStatus":status,"sourceBatches":[]string{stored.Payload.PacketID},"payload":stored.Payload,"locator":stored.Locator,"signature":sig}
		if verification!=nil{result["verification"]=verification}
		writeJSON(w,200,result)
	})
	admin.HandleFunc("POST /v1/ui/batches/{packetId}/verify",func(w http.ResponseWriter,r *http.Request){
		result,err:=verifyGridBatch(r.Context(),db,r.PathValue("packetId"),keyDir,chainURL,token); if err!=nil { uiFailure(w,err); return }; writeJSON(w,201,result)
	})
	admin.HandleFunc("GET /v1/ui/batches/{packetId}/verifications",func(w http.ResponseWriter,r *http.Request){
		page,err:=queryPage(r.Context(),db,r.URL.Query(),`SELECT id,checked_at AS checkedAt,status,result_json AS result FROM grid_verification_runs WHERE packet_id=? ORDER BY checked_at DESC,id DESC`,`SELECT COUNT(*) FROM grid_verification_runs WHERE packet_id=?`,r.PathValue("packetId")); if err!=nil { uiFailure(w,err); return };for _,item:=range page.Items{var result map[string]any;raw,_:=item["result"].(string);_ = json.Unmarshal([]byte(raw),&result);item["observer"]=result["observer"];item["ruleVersion"]=result["ruleVersion"];item["message"]=fmt.Sprint(result["status"])+"；本地 "+fmt.Sprint(result["computedRoot"])+" / 链上 "+fmt.Sprint(result["chainRoot"]);item["result"]=result};writeJSON(w,200,page)
	})
	mux.Handle("/v1/ui/",controlAuth(token,admin))
	server:=&http.Server{Addr:env("GRID_HOST","0.0.0.0")+":"+env("GRID_PORT","8083"),Handler:mux,ReadHeaderTimeout:5*time.Second}
	log.Printf("GRID listening address=%s schema=%s realBlockchain=false",server.Addr,env("GRID_SCHEMA","grid_data"))
	go func(){<-ctx.Done();shutdown,cancel:=context.WithTimeout(context.Background(),5*time.Second);defer cancel();_ = server.Shutdown(shutdown)}()
	err=server.ListenAndServe(); if err==http.ErrServerClosed{return nil}; return err
}

func storeGridBatch(ctx context.Context,db *sql.DB,e GridEnvelope,payloadHash string)(bool,error){
	tx,err:=db.BeginTx(ctx,nil); if err!=nil{return false,err}; defer tx.Rollback()
	var oldHash string
	err=tx.QueryRowContext(ctx,`SELECT payload_hash FROM grid_batches WHERE packet_id=? FOR UPDATE`,e.Batch.Payload.PacketID).Scan(&oldHash)
	if err==nil { if oldHash!=payloadHash{return false,fmt.Errorf("packetId already exists with different content")}; return true,tx.Commit() }
	if err!=sql.ErrNoRows{return false,err}
	p:=e.Batch.Payload; raw,_:=Canonical(map[string]any{"payload":p,"locator":e.Locator,"vppId":e.VPPID})
	if _,err=tx.ExecContext(ctx,`INSERT INTO grid_batches(packet_id,vpp_id,edge_id,batch_id,tx_hash,block_number,merkle_root,record_count,payload_hash,payload_json,signature,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,p.PacketID,e.VPPID,p.EdgeID,e.Locator.BatchID,e.Locator.TxHash,e.Locator.BlockNumber,e.Locator.MerkleRoot,p.RecordCount,payloadHash,string(raw),e.Batch.Signature,nowUTC());err!=nil{return false,err}
	for _,record:=range p.Records { b,_:=Canonical(record); if _,err=tx.ExecContext(ctx,`INSERT INTO grid_records(record_id,packet_id,device_id,device_type,collected_at,payload_json) VALUES(?,?,?,?,?,?)`,record.RecordID,p.PacketID,record.DeviceID,record.DeviceType,record.Timestamp,string(b));err!=nil{return false,err} }
	details,_:=json.Marshal(map[string]any{"vppId":e.VPPID,"edgeId":p.EdgeID,"recordCount":p.RecordCount,"txHash":e.Locator.TxHash,"blockNumber":e.Locator.BlockNumber})
	if _,err=tx.ExecContext(ctx,`INSERT INTO grid_events(packet_id,event_type,occurred_at,details_json) VALUES(?,'BATCH_PERSISTED',?,?)`,p.PacketID,nowUTC(),string(details));err!=nil{return false,err}
	return false,tx.Commit()
}

func gridFilters(q url.Values,alias string)([]string,[]any,error){
	clauses:=[]string{};args:=[]any{}
	for key,column:=range map[string]string{"packetId":alias+".packet_id","edgeId":alias+".edge_id","vppId":alias+".vpp_id"}{if value:=strings.TrimSpace(q.Get(key));value!=""{clauses=append(clauses,column+"=?");args=append(args,value)}}
	if alias=="r"{if value:=strings.TrimSpace(q.Get("deviceId"));value!=""{clauses=append(clauses,"r.device_id=?");args=append(args,value)}}
	column:=alias+".received_at";if alias=="r"{column="r.collected_at"}
	timeClauses,timeArgs,err:=timeFilters(q,column);if err!=nil{return nil,nil,err};clauses=append(clauses,timeClauses...);args=append(args,timeArgs...)
	return clauses,args,nil
}

func gridRecordFilters(q url.Values)([]string,[]any,error){
	clauses:=[]string{};args:=[]any{}
	for key,column:=range map[string]string{"packetId":"r.packet_id","edgeId":"b.edge_id","vppId":"b.vpp_id","deviceId":"r.device_id"}{if value:=strings.TrimSpace(q.Get(key));value!=""{clauses=append(clauses,column+"=?");args=append(args,value)}}
	timeClauses,timeArgs,err:=timeFilters(q,"r.collected_at");if err!=nil{return nil,nil,err};clauses=append(clauses,timeClauses...);args=append(args,timeArgs...)
	return clauses,args,nil
}

func gridRecordsPage(w http.ResponseWriter,r *http.Request,db *sql.DB,packetID string){
	q:=r.URL.Query();if packetID!=""{q.Set("packetId",packetID)}
	clauses,args,err:=gridRecordFilters(q);if err!=nil{uiFailure(w,err);return};where:="";if len(clauses)>0{where=" WHERE "+strings.Join(clauses," AND ")}
	query:=`SELECT r.record_id AS recordId,r.packet_id AS packetId,b.vpp_id AS vppId,b.edge_id AS edgeId,r.device_id AS deviceId,r.device_type AS deviceType,r.collected_at AS collectedAt,r.payload_json AS record FROM grid_records r JOIN grid_batches b ON b.packet_id=r.packet_id`+where+` ORDER BY r.collected_at DESC,r.record_id DESC`
	count:=`SELECT COUNT(*) FROM grid_records r JOIN grid_batches b ON b.packet_id=r.packet_id`+where
	page,err:=queryPage(r.Context(),db,q,query,count,args...);if err!=nil{uiFailure(w,err);return}
	for _,item:=range page.Items{raw,_:=item["record"].(string);var record Record;if json.Unmarshal([]byte(raw),&record)==nil{item["record"]=record}}
	writeJSON(w,200,page)
}

func verifyGridBatch(ctx context.Context,db *sql.DB,packetID,keyDir,chainURL,token string)(map[string]any,error){
	result:=map[string]any{"observer":"GRID","method":"本地核验整批记录、边缘签名与 VPP 模拟链摘要","ruleVersion":"prototype-v1","realBlockchain":false}
	var envelope struct { VPPID string `json:"vppId"`; Payload Payload `json:"payload"`; Locator ChainLocator `json:"locator"` }; var signature string; var storedHash,storedRoot string
	var checks []map[string]any
	check:=func(name string,ok bool,message string){state:="FAIL";if ok{state="PASS"};checks=append(checks,map[string]any{"name":name,"status":state,"message":message})}
	var raw string
	if err:=db.QueryRowContext(ctx,`SELECT payload_json,signature,payload_hash,merkle_root FROM grid_batches WHERE packet_id=?`,packetID).Scan(&raw,&signature,&storedHash,&storedRoot);err!=nil{return nil,err}
	if err:=json.Unmarshal([]byte(raw),&envelope);err!=nil{return nil,err}
	p:=envelope.Payload; canonical,canonErr:=Canonical(p); hash:=sha256.Sum256(canonical)
	check("签名包结构与编号",ValidatePayload(p)==nil && p.PacketID==packetID,"校验包格式、记录数和记录编号")
	check("保存内容完整性",canonErr==nil && hex.EncodeToString(hash[:])==storedHash,"签名包规范化内容与接收时哈希一致")
	pub,err:=os.ReadFile(filepath.Join(keyDir,p.EdgeID+".pub")); sigOK:=false
	if err==nil && canonErr==nil { sigOK=Verify(string(pub),canonical,signature)==nil }
	check("边缘签名",sigOK,"使用可信 Edge 公钥验证原始签名")
	storedRecords:=map[string]string{};rows,err:=db.QueryContext(ctx,`SELECT record_id,payload_json FROM grid_records WHERE packet_id=? ORDER BY record_id`,packetID);if err!=nil{return nil,err};for rows.Next(){var id,recordJSON string;if err=rows.Scan(&id,&recordJSON);err!=nil{rows.Close();return nil,err};storedRecords[id]=recordJSON};rows.Close()
	consistent:=len(storedRecords)==len(p.Records);for _,record:=range p.Records{expected,_:=Canonical(record);var actual Record;rawRecord,ok:=storedRecords[record.RecordID];if !ok||json.Unmarshal([]byte(rawRecord),&actual)!=nil{consistent=false;continue};stored,_:=Canonical(actual);if !bytes.Equal(expected,stored){consistent=false}}
	check("保存记录与批次原文一致",consistent,"逐条比对独立保存的完整记录内容")
	root,rootErr:=MerkleRoot(p.Records);check("本地 Merkle 摘要",rootErr==nil && root==storedRoot && root==envelope.Locator.MerkleRoot,"按既有确定性规则重算批次摘要")
	query,err:=url.Parse(chainURL);if err!=nil{return nil,err};q:=query.Query();q.Set("batchId",envelope.Locator.BatchID);q.Set("txHash",envelope.Locator.TxHash);q.Set("blockNumber",fmt.Sprint(envelope.Locator.BlockNumber));query.RawQuery=q.Encode()
	req,err:=http.NewRequestWithContext(ctx,http.MethodGet,query.String(),nil);if err!=nil{return nil,err};req.Header.Set("Authorization","Bearer "+token)
	client:=&http.Client{Timeout:5*time.Second};resp,err:=client.Do(req)
	chainRoot:=""; chainOK:=false;chainQueryFailed:=err!=nil;chainQueryMessage:=""
	if err==nil { defer resp.Body.Close(); if resp.StatusCode==200 { var summary map[string]any; if decodeErr:=json.NewDecoder(resp.Body).Decode(&summary);decodeErr==nil { chainRoot,_=summary["merkleRoot"].(string); chainOK=summary["batchId"]==envelope.Locator.BatchID && summary["txHash"]==envelope.Locator.TxHash && numeric(summary["blockNumber"])==int(envelope.Locator.BlockNumber) && summary["packetId"]==packetID && summary["edgeId"]==p.EdgeID && numeric(summary["recordCount"])==p.RecordCount && summary["windowStart"]==p.WindowStart && summary["windowEnd"]==p.WindowEnd && summary["status"]=="SIMULATED_REGISTERED" && chainRoot==root } else {chainQueryFailed=true;chainQueryMessage="VPP 摘要响应无法解析"} } else if resp.StatusCode!=404 {chainQueryFailed=true;chainQueryMessage=fmt.Sprintf("VPP 摘要查询返回 HTTP %d",resp.StatusCode)} }
	chainCheck:=map[string]any{"name":"链定位与摘要比对","status":"FAIL","message":"按 txHash、blockNumber、batchId 查询 VPP 模拟链摘要并比对"};if chainOK{chainCheck["status"]="PASS"};if chainQueryFailed{chainCheck["status"]="ERROR";chainCheck["message"]="VPP 链摘要查询不可用；本次结论未知";if chainQueryMessage!=""{chainCheck["message"]=chainQueryMessage}};checks=append(checks,chainCheck)
	status:="PASS";for _,c:=range checks{if c["status"]!="PASS"{status="FAIL"}}
	if chainQueryFailed {status="ERROR"}
	result["checks"],result["computedRoot"],result["chainRoot"],result["status"],result["locator"]=checks,root,chainRoot,status,envelope.Locator
	if status=="ERROR"{result["failureReason"]="链上摘要查询不可用，无法确认完整性"}else if status=="FAIL"{result["failureReason"]="至少一项本地批次或摘要检查未通过"}
	id:=NewPacketID("grid-verify");checked:=nowUTC();encoded,_:=json.Marshal(result)
	if _,err=db.ExecContext(ctx,`INSERT INTO grid_verification_runs(id,packet_id,checked_at,status,result_json) VALUES(?,?,?,?,?)`,id,packetID,checked,status,string(encoded));err!=nil{return nil,err}
	return map[string]any{"id":id,"packetId":packetID,"checkedAt":checked,"status":status,"result":result},nil
}

func DeliverGridLoop(ctx context.Context,db *sql.DB,gridURL,token string){
	client:=&http.Client{Timeout:15*time.Second};ticker:=time.NewTicker(time.Second);defer ticker.Stop()
	for{select{case<-ctx.Done():return;case<-ticker.C:
		items,err:=DueGridDeliveries(db);if err!=nil{log.Printf("GRID outbox query failed: %v",err);continue}
		for _,item:=range items{req,err:=http.NewRequestWithContext(ctx,http.MethodPost,gridURL,bytes.NewBufferString(item["envelope"]));if err==nil{req.Header.Set("Content-Type","application/json");req.Header.Set("Authorization","Bearer "+token);var resp *http.Response;resp,err=client.Do(req);if err==nil{resp.Body.Close();if resp.StatusCode==200||resp.StatusCode==201{err=UpdateGridDelivery(db,item["packetId"],true,"",0);if err==nil{log.Printf("GRID delivery confirmed packetId=%s",item["packetId"])};continue};err=fmt.Errorf("GRID HTTP %d",resp.StatusCode)}};attempt,_:=strconv.Atoi(item["attempts"]);delay:=time.Second*time.Duration(1<<min(attempt,6));if delay>time.Minute{delay=time.Minute};_ = UpdateGridDelivery(db,item["packetId"],false,err.Error(),delay);log.Printf("GRID delivery deferred packetId=%s attempt=%d retrySeconds=%d error=%v",item["packetId"],attempt+1,int(delay.Seconds()),err)}
	}}
}
