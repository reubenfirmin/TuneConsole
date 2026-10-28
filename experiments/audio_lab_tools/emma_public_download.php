<?php
/** Retrieve EMMA's public CSV through the same Shiny download as its website.
 * No authentication, private endpoint, or user account is involved.
 * Run inside the existing PHP/curl research downloader container.
 */
declare(strict_types=1);
$host = 'musemap-tools.uibk.ac.at';
$context = stream_context_create(['ssl' => ['verify_peer' => true, 'verify_peer_name' => true]]);
$socket = stream_socket_client("tls://$host:443", $errno, $error, 15, STREAM_CLIENT_CONNECT, $context);
if (!$socket) throw new RuntimeException($error);
stream_set_timeout($socket, 25);
$path = '/emma/__sockjs__/n=' . bin2hex(random_bytes(9)) . '/000/' . bin2hex(random_bytes(6)) . '/websocket';
$key = base64_encode(random_bytes(16));
fwrite($socket, "GET $path HTTP/1.1\r\nHost: $host\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\nOrigin: https://$host\r\n\r\n");
$headers = '';
while (($line = fgets($socket)) !== false) {
    $headers .= $line;
    if ($line === "\r\n") break;
}
if (!str_starts_with($headers, 'HTTP/1.1 101')) throw new RuntimeException('WebSocket upgrade failed: ' . strtok($headers, "\r\n"));
$accept = base64_encode(sha1($key . '258EAFA5-E914-47DA-95CA-C5AB0DC85B11', true));
if (!str_contains($headers, $accept)) throw new RuntimeException('Invalid WebSocket accept');
function sendFrame($socket, string $text, int $opcode = 1): void {
    $mask = random_bytes(4);
    $n = strlen($text);
    $header = chr(0x80 | $opcode) . ($n < 126 ? chr(0x80 | $n) : chr(0xfe) . pack('n', $n));
    $masked = '';
    for ($i = 0; $i < $n; $i++) $masked .= $text[$i] ^ $mask[$i % 4];
    fwrite($socket, $header . $mask . $masked);
}
function readExact($socket, int $n): string {
    $result = '';
    while (strlen($result) < $n) {
        $part = fread($socket, $n - strlen($result));
        if ($part === false || $part === '') throw new RuntimeException('Socket closed or timed out');
        $result .= $part;
    }
    return $result;
}
$initial = ['method' => 'init', 'data' => ['l' => 'EN', 'hide_explicit' => false, 'main_navbar' => 'tab2',
    'level_select' => '2', 'genre_select' => 'no_filter', 'instr_select' => 'no_filter', 'lang_select' => '1',
    'toggle_sources' => false,
    '.clientdata_url_protocol' => 'https:', '.clientdata_url_hostname' => $host,
    '.clientdata_url_pathname' => '/emma/', '.clientdata_url_search' => '', '.clientdata_url_hash_initial' => '',
    '.clientdata_pixelratio' => 1, '.clientdata_output_downloadCSV_hidden' => false]];
$deadline = time() + 90;
$fragments = '';
$received = 0;
while (time() < $deadline) {
    $first = readExact($socket, 2);
    $opcode = ord($first[0]) & 15;
    $n = ord($first[1]) & 127;
    if ($n === 126) $n = unpack('n', readExact($socket, 2))[1];
    if ($n === 127) $n = unpack('J', readExact($socket, 8))[1];
    if ($n > 20000000) throw new RuntimeException('Unexpected oversized frame');
    $payload = readExact($socket, $n);
    if ($opcode === 9) { sendFrame($socket, $payload, 10); continue; }
    if ($opcode === 8) throw new RuntimeException('Server closed public session');
    $fragments .= $payload;
    if (!(ord($first[0]) & 0x80)) continue;
    $message = $fragments;
    $fragments = '';
    if ($message === 'o') {
        sendFrame($socket, json_encode(['0#0|o|']));
        sendFrame($socket, json_encode(['1#0|m|' . json_encode($initial)]));
        continue;
    }
    if (!str_starts_with($message, 'a')) continue;
    foreach (json_decode(substr($message, 1), true, 512, JSON_THROW_ON_ERROR) as $packet) {
        if (preg_match('/^([0-9A-F]+)#0\|m\|(.*)$/s', $packet, $match)) {
            $received++;
            file_put_contents('/data/public-shiny-messages.jsonl', $match[2] . "\n", FILE_APPEND);
            sendFrame($socket, json_encode(['ACK ' . strtoupper(dechex(hexdec($match[1]) + 1))]));
            $data = json_decode($match[2], true);
            $href = $data['values']['downloadCSV'] ?? null;
            if (is_string($href) && $href !== '') {
                $url = str_starts_with($href, 'http') ? $href : "https://$host/emma/" . ltrim($href, '/');
                if (!str_starts_with($url, "https://$host/")) throw new RuntimeException('Unexpected download host');
                $curl = curl_init($url);
                curl_setopt_array($curl, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 25, CURLOPT_FAILONERROR => true]);
                $csv = curl_exec($curl);
                if ($csv === false) throw new RuntimeException(curl_error($curl));
                file_put_contents('/data/current-public-download.csv', $csv);
                echo json_encode(['messages' => $received, 'download_bytes' => strlen($csv), 'sha256' => hash('sha256', $csv)]) . "\n";
                sendFrame($socket, pack('n', 1000), 8);
                fclose($socket);
                exit(0);
            }
        }
    }
}
throw new RuntimeException('Public Shiny session supplied no download link');
