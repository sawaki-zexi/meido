/* Native argv/envelope fixture; no network and no child processes. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#include <shellapi.h>
#else
#include <unistd.h>
#endif
static void quote(FILE *f, const char *s) {
    fputc('"', f);
    for (; *s; s++) {
        if (*s == '\\' || *s == '"') { fputc('\\', f); fputc(*s, f); }
        else if (*s == '\n') fputs("\\n", f);
        else if (*s == '\r') fputs("\\r", f);
        else if (*s == '\t') fputs("\\t", f);
        else fputc(*s, f);
    }
    fputc('"', f);
}
int main(int argc, char **argv) {
#ifdef _WIN32
    LPWSTR *wide = CommandLineToArgvW(GetCommandLineW(), &argc);
    argv = calloc(argc, sizeof(char *));
    for (int i = 0; i < argc; i++) {
        int n = WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, NULL, 0, NULL, NULL);
        argv[i] = malloc(n);
        WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, argv[i], n, NULL, NULL);
    }
#endif
    const char *root = getenv("MEIDO_HERDR_FIXTURE");
    if (!root) return 2;
    char path[8192];
    snprintf(path, sizeof(path), "%s/calls.jsonl", root);
    FILE *log = fopen(path, "ab");
    fputc('[', log);
    for (int i = 1; i < argc; i++) { if (i > 1) fputc(',', log); quote(log, argv[i]); }
    fputs("]\n", log); fclose(log);
    const char *name = NULL;
    if (argc == 2 && !strcmp(argv[1], "--version")) name = "version";
    else if (argc > 3 && !strcmp(argv[3], "status")) name = "server";
    else if (argc > 3 && !strcmp(argv[3], "workspace")) name = "workspaces";
    else if (argc > 4 && !strcmp(argv[4], "list")) name = "agents";
    else if (argc > 4 && !strcmp(argv[4], "get")) name = "get";
    else if (argc > 4 && !strcmp(argv[4], "read")) name = "read";
    else if (argc > 4 && !strcmp(argv[4], "prompt")) name = "prompt";
    else if (argc > 4 && !strcmp(argv[4], "send-keys")) name = "keys";
    else return 3; /* No server start/stop or agent creation protocol exists. */
    snprintf(path, sizeof(path), "%s/%s.txt", root, name);
    FILE *response = fopen(path, "rb");
    if (!response) return 4;
    fseek(response, 0, SEEK_END); long size = ftell(response); rewind(response);
    char *body = malloc(size + 1); size_t length = fread(body, 1, size, response); fclose(response);
    snprintf(path, sizeof(path), "%s/%s.delay", root, name);
    FILE *delay = fopen(path, "r");
    if (delay) { unsigned int ms = 0; fscanf(delay, "%u", &ms); fclose(delay);
#ifdef _WIN32
        Sleep(ms);
#else
        usleep(ms * 1000);
#endif
    }
    fwrite(body, 1, length, stdout); free(body);
    return 0;
}
